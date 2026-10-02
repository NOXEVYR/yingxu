import json
from pathlib import Path
import tempfile
import unittest

from yingxu.settings import Settings
from yingxu.store import UserError


class WorkbenchSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='yingxu-workbench-settings-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = Settings(self.root)

    def test_new_workspace_defaults_are_read_only_until_explicit_save(self):
        values = self.settings.get()
        self.assertEqual(values['appearance_theme'], 'swiss')
        self.assertEqual(values['workspace_layout'], 'focus')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_six_light_themes_and_both_layouts_survive_reopen(self):
        for theme in ('swiss', 'graphite', 'paper', 'pine', 'ocean', 'plum'):
            for layout in ('classic', 'focus'):
                with self.subTest(theme=theme, layout=layout):
                    self.settings.update({'appearance_theme': theme, 'workspace_layout': layout})
                    reloaded = Settings(self.root).get()
                    self.assertEqual(reloaded['appearance_theme'], theme)
                    self.assertEqual(reloaded['workspace_layout'], layout)

    def test_old_three_theme_settings_gain_layout_without_rewriting_file(self):
        for theme in ('swiss', 'pine', 'paper'):
            with self.subTest(theme=theme):
                old = {'appearance_theme': theme, 'capture_mode': 'quick',
                       'default_view': 'list', 'confirm_delete': False,
                       'automatic_update_check': False}
                self.settings.path.write_text(json.dumps(old), encoding='utf-8')
                before = self.settings.path.read_bytes()
                values = self.settings.get()
                self.assertEqual(values['workspace_layout'], 'focus')
                for key, value in old.items():
                    self.assertEqual(values[key], value)
                self.assertEqual(self.settings.path.read_bytes(), before)
                self.settings.update({'workspace_layout': 'classic'})
                saved = Settings(self.root).get()
                self.assertEqual(saved['workspace_layout'], 'classic')
                for key, value in old.items():
                    self.assertEqual(saved[key], value)

    def test_invalid_layout_or_theme_rejects_entire_patch_and_preserves_file(self):
        self.settings.update({'appearance_theme': 'paper', 'workspace_layout': 'classic'})
        before = self.settings.path.read_bytes()
        invalid = ('', 'dark', 'FOCUS', None, True, 0, [], {})
        for key in ('workspace_layout', 'appearance_theme'):
            for value in invalid:
                with self.subTest(key=key, value=value), self.assertRaises(UserError):
                    self.settings.update({key: value, 'confirm_delete': False})
                self.assertEqual(self.settings.path.read_bytes(), before)
                self.assertEqual(list(self.root.glob('.settings-*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
