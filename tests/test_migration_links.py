from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote

from yingxu.migration_links import rewrite_markdown


class MigrationLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yingxu-migration-links-')
        self.root = Path(self.tmp.name).resolve()
        self.old = self.root/'old'
        self.new = self.root/'new'
        self.source = self.old/'00_Brief/剧本与文档/第1集/剧本.md'
        self.target = self.new/'文本/第1集/剧本.md'
        self.image = self.old/'20_Assets/角色/人物 图(1).png'
        self.image.parent.mkdir(parents=True);self.image.write_bytes(b'synthetic')
        self.source.parent.mkdir(parents=True);self.source.touch()
        self.attachment = self.old/'10_References/说明.md'
        self.attachment.parent.mkdir(parents=True);self.attachment.write_bytes(b'content')
        self.mapping = {self.source:self.target,self.image:self.new/'角色/人物 图(1).png',
                        self.attachment:self.new/'记录/说明.md'}
        self.before = '../../../20_Assets/角色/人物 图(1).png'
        self.after = quote('../../角色/人物 图(1).png',safe='/.-_~')

    def tearDown(self):self.tmp.cleanup()

    def rewrite(self,text):
        return rewrite_markdown(text.encode('utf-8'),self.source,self.target,self.mapping)

    def test_encoded_link_image_title_query_fragment_and_stable_id(self):
        encoded = quote(self.before,safe='/.-_~')
        suffix = '?download=1&amp;size=2#yx-item='+'a'*32
        text = f'![角色]({encoded}{suffix} "标题")\n[附件](../../../10_References/说明.md#yx-item=abc)\n'
        result = self.rewrite(text)
        self.assertTrue(result['changed']);self.assertEqual(result['warnings'],[])
        expected = f'![角色]({self.after}{suffix} "标题")\n[附件]({quote("../../记录/说明.md",safe="/.-_~")}#yx-item=abc)\n'
        self.assertEqual(result['content'].decode(),expected)

    def test_angle_destinations_escaped_punctuation_and_reference_definitions(self):
        # CommonMark only punctuation escapes are supported; spaces use angle URLs.
        text = f'![角色](<{self.before}> \'标题\')\n[图]: <{self.before}> "说明"\n[角色][图]\n'
        result = self.rewrite(text)
        self.assertEqual(result['content'].decode(),f'![角色](<{self.after}> \'标题\')\n[图]: <{self.after}> "说明"\n[角色][图]\n')
        no_space = self.image.with_name('人物(2).png');no_space.write_bytes(b'synthetic')
        self.mapping[no_space] = self.new/'角色/人物(2).png'
        result = self.rewrite(r'[角色](../../../20_Assets/角色/人物\(2\).png)')
        self.assertEqual(result['content'].decode(),f'[角色]({quote("../../角色/人物(2).png",safe="/.-_~")})')

    def test_numeric_entities_in_paths_are_not_confused_with_url_fragments(self):
        text = '![图](<../../../20_Assets/角色/人物&#32;图(1).png?x=1&amp;y=2#anchor>)'
        result = self.rewrite(text)
        self.assertEqual(result['content'].decode(),f'![图](<{self.after}?x=1&amp;y=2#anchor>)')
        text = '![图](<../../../20_Assets/角色/人物&#x20;图(1).png&quest;download=1&num;anchor>)'
        result = self.rewrite(text)
        self.assertEqual(result['content'].decode(),f'![图](<{self.after}&quest;download=1&num;anchor>)')

    def test_balanced_parentheses_nested_labels_and_two_adjacent_links(self):
        path = self.image.with_name('人物(2).png');path.write_bytes(b'synthetic')
        self.mapping[path] = self.new/'角色/人物(2).png'
        text = '[嵌套[标签]](../../../20_Assets/角色/人物(2).png) [图](<'+self.before+'>)'
        result = self.rewrite(text)
        self.assertEqual(result['content'].decode(),f'[嵌套[标签]]({quote("../../角色/人物(2).png",safe="/.-_~")}) [图](<{self.after}>)')

    def test_code_fences_inline_code_html_and_escaped_links_are_never_rewritten(self):
        link = f'![角色](<{self.before}>)'
        raw = ('```md\r\n'+link+'\r\n```\r\n~~~\r\n'+link+'\r\n~~~\r\n'
               +'`'+link+'`\r\n``a` '+link+'``\r\n'
               +'`跨行\r\n'+link+'`\r\n'
               +'<!-- '+link+' -->\r\n<pre>'+link+'</pre>\r\n'
               +'<span title="'+link+'">literal</span>\r\n'
               +'\\'+link[1:]+'\r\n')
        result = self.rewrite(raw)
        self.assertFalse(result['changed']);self.assertEqual(result['content'],raw.encode())

    def test_utf8_bom_utf16_endianness_gb18030_and_mixed_newlines_preserved(self):
        old = f'标题\r\n![角色](<{self.before}>)\n尾行\r'
        new = f'标题\r\n![角色](<{self.after}>)\n尾行\r'
        for encoding,bom in [('utf-8',b''),('utf-8',b'\xef\xbb\xbf'),('utf-16-le',b'\xff\xfe'),
                             ('utf-16-be',b'\xfe\xff'),('gb18030',b'')]:
            with self.subTest(encoding=encoding,bom=bom):
                result = rewrite_markdown(bom+old.encode(encoding),self.source,self.target,self.mapping)
                self.assertEqual(result['content'],bom+new.encode(encoding));self.assertTrue(result['changed'])

    def test_external_absolute_unmapped_and_missing_destinations_preserved(self):
        missing = self.image.with_name('missing.png');self.mapping[missing]=self.new/'角色/missing.png'
        text = ('[网络](https://example.org/p.png?q=1#x) [本地](file:///C:/file.png) '
                '[绝对](/image.png) [盘符](D:/file.png) [锚点](#header) '
                '[缺失](../../../20_Assets/角色/missing.png) [无对应](other.md)')
        result = self.rewrite(text)
        self.assertEqual(result['content'],text.encode());self.assertFalse(result['changed']);self.assertTrue(result['warnings'])

    def test_same_relative_layout_retains_exact_original_escaping(self):
        self.mapping[self.image] = self.new/'20_Assets/角色/人物 图(1).png'
        self.target = self.new/'00_Brief/剧本与文档/第1集/剧本.md'
        text = f'![x](<{self.before}> "keep")'
        result = self.rewrite(text)
        self.assertEqual(result['content'],text.encode());self.assertFalse(result['changed'])

    def test_ambiguous_multiline_and_invalid_encoded_paths_warn_without_corruption(self):
        text = '[x](\n<'+self.before+'>\n)\n[id]:\n  <'+self.before+'>\n[bad](a%ZZ.png)\n'
        result = self.rewrite(text)
        self.assertEqual(result['content'],text.encode());self.assertTrue(result['warnings'])

    def test_non_markdown_and_large_files_are_not_transformed(self):
        raw = f'[x](<{self.before}>)'.encode()
        result = rewrite_markdown(raw,self.source.with_suffix('.txt'),self.target,self.mapping)
        self.assertEqual(result['content'],raw);self.assertFalse(result['changed'])
        result = rewrite_markdown(b'a'*(2*1024*1024+1),self.source,self.target,self.mapping)
        self.assertFalse(result['changed']);self.assertTrue(result['warnings'])


if __name__ == '__main__':unittest.main()
