import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import package_release


class PackageManifestBindingTests(unittest.TestCase):
    def test_external_manifest_binds_exact_utf8_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'desktop').mkdir()
            (root/'desktop/brand.ico').write_bytes(b'fixture icon')
            source = root/'fixture.py'; source.write_text('# fixture', encoding='utf-8')
            with patch.object(package_release, 'ROOT', root), \
                    patch.object(package_release, 'files_to_package', return_value=[source]), \
                    patch.object(package_release, 'runtime_files', return_value=[]), \
                    patch('sys.argv', ['package_release', '--output-dir', str(root/'release')]):
                package_release.main()
            external = json.loads(next((root/'release').glob('*manifest.json')).read_text(encoding='utf-8'))
            archive_path = root/'release'/external['file']
            with zipfile.ZipFile(archive_path) as archive:
                raw = archive.read('YingXu/RELEASE_MANIFEST.json')
                self.assertEqual(external['release_manifest_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(archive.read('YingXu/fixture.py'), source.read_bytes())
            self.assertEqual(external['sha256'], hashlib.sha256(archive_path.read_bytes()).hexdigest())
