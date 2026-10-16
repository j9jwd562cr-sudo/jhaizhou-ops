import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
from source.configure import configure
from source.resolve import resolve_ref
from fixtures import git, source


class SourceTests(unittest.TestCase):
    def test_latest_release_and_explicit_source_selection(self):
        with patch('source.resolve.subprocess.check_output', side_effect=[
                json.dumps({'tag_name': 'v1.2.3', 'draft': False, 'prerelease': False}),
                json.dumps({'tag_name': 'v1.2.4', 'draft': False, 'prerelease': False})]):
            self.assertEqual(resolve_ref('owner/upstream'), 'refs/tags/v1.2.3')
            self.assertEqual(resolve_ref('owner/upstream'), 'refs/tags/v1.2.4')
        with patch('source.resolve.subprocess.check_output') as lookup:
            self.assertEqual(resolve_ref('owner/fork', 'main'), 'main')
            lookup.assert_not_called()
        with patch('source.resolve.subprocess.check_output', return_value=json.dumps({'tag_name': 'v1.3-beta', 'prerelease': True, 'draft': False})):
            with self.assertRaises(ValueError):
                resolve_ref('owner/upstream')

    def test_versions_repeat_builds_and_runtime_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, commit, programs, _ = source(root)
            configs = []
            for run, attempt in (('7', '1'), ('7', '2'), ('8', '1')):
                with patch.dict(os.environ, GITHUB_RUN_NUMBER=run, GITHUB_RUN_ATTEMPT=attempt):
                    config = configure(root)
                self.assertEqual(config['app_version'], '1.2.3')
                self.assertEqual(config['nso_sha256'], programs)
                configs.append(config)
            self.assertEqual(len({ci.ipa_names(c) for c in configs}), 3)
            self.assertEqual(ci.ipa_names(configs[0])[0], 'D2RMobile-v1.2.3-build.7.1.ipa')
            (upstream / 'dm/runtime.c').write_text('new runtime\n')
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-m', 'runtime update')
            with patch.dict(os.environ, GITHUB_RUN_NUMBER='9'):
                current = configure(root)
            self.assertIn('-src-' + git(upstream, 'rev-parse', '--short=12', 'HEAD'), current['build_label'])
            self.assertNotEqual(current['upstream_commit'], commit)
