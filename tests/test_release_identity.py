import unittest

from yingxu.release_identity import compare_builds, parse_build_revision, public_build


class ReleaseIdentityTests(unittest.TestCase):
    def test_only_same_series_numeric_revision_is_ordered(self):
        for latest, expected in [('workflow.3', 'build'), ('workflow.2', 'current'),
                                 ('workflow.1', 'current'), ('patch.3', 'manual')]:
            self.assertEqual(compare_builds('workflow.2', latest), expected)
        self.assertEqual(compare_builds('workflow.9', 'workflow.10'), 'build')

    def test_unknown_or_malformed_never_ordered_or_exposed(self):
        for value in [None, {}, 3, '', 'workflow', 'workflow.03', 'workflow.-1', 'workflow.3x',
                      'workflow.٣', 'Workflow.3', 'x' * 33 + '.3', 'workflow.1000000000', 'workflow.3\n']:
            self.assertIsNone(parse_build_revision(value), value)
            self.assertEqual(public_build(value), '')
            self.assertEqual(compare_builds(value, 'workflow.3'), 'manual')
            self.assertEqual(compare_builds('workflow.2', value), 'manual')
        self.assertEqual(parse_build_revision('workflow.0'), ('workflow', 0))
