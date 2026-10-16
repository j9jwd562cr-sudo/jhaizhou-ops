import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
from source import publish


def reports(root):
    out = root / 'outputs'
    out.mkdir()
    config = {'upstream_commit': 'a' * 40, 'app_version': '1.2.3', 'build_number': '7.1',
              'build_label': 'v1.2.3', 'release_tag': 'v1.2.3-build.7.1'}
    common = {'source_commit': config['upstream_commit'], 'tools_commit': 'b' * 40,
              'product': 'fixture-product', 'payload_sha256': 'c' * 64,
              'legacy_pack': {'name': 'fixture'}, 'app_version': '1.2.3', 'build_number': '7.1',
              'release_tag': config['release_tag'], 'shader_chain': 'chain', 'metal_toolchain': {},
              'prepared_archive_sha256': 'd' * 64, 'resource_manifest_sha256': 'e' * 64,
              'runtime_resources_sha256': {'dm/shader_map.tsv': 'f' * 64},
              'native_built_from_source': True, 'payload_verified': True, 'metal_precompiled': True,
              'phone_shader_compilation_required': False}
    for name, minimum, metadata in zip(ci.ipa_names(config), ('26.0', '18.0'),
                                        ('source-report.json', 'source-ios18-report.json')):
        path = out / name
        path.write_bytes(name.encode())
        report = dict(common, ipa_name=name, minimum_os=minimum, native_minimum_os=minimum + '.0',
                      ipa_bytes=path.stat().st_size, ipa_sha256=ci.sha256(path))
        (out / metadata).write_text(json.dumps(report))
    return out, config


class ReleaseStore:
    def __init__(self):
        self.release = None
        self.calls = []
        self.corrupt_upload = False
        self.lock = threading.Lock()

    def gh(self, *args):
        self.calls.append(args)
        action = args[1]
        if action == 'create':
            self.release = {'draft': True, 'assets': [], 'html_url': 'https://github.com/owner/repo/releases/tag/test'}
        elif action == 'upload':
            path = Path(args[3])
            with self.lock:
                self.release['assets'] = [a for a in self.release['assets'] if a['name'] != path.name]
                self.release['assets'].append({'name': path.name, 'size': path.stat().st_size, 'state': 'uploaded',
                                              'digest': 'sha256:' + ('0' * 64 if self.corrupt_upload else ci.sha256(path))})
        elif action == 'edit':
            self.release['draft'] = False
        else:
            raise AssertionError(args)
        return ''


class PublishTests(unittest.TestCase):
    def test_release_has_verified_variants_and_retry_reuses_matching_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out, config = reports(root)
            store = ReleaseStore()
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config), patch.object(publish, 'release_info', side_effect=lambda *a: store.release), patch.object(publish, 'gh', side_effect=store.gh), patch.dict(os.environ, GITHUB_REPOSITORY='owner/repo', GITHUB_SHA='b' * 40, GITHUB_STEP_SUMMARY=''):
                release = publish.main()
                self.assertFalse(release['draft'])
                self.assertEqual({a['name'] for a in release['assets']}, {*ci.ipa_names(config), 'SHA256SUMS.txt', 'build-report.json'})
                self.assertIn('## 简体中文', (out / 'RELEASE.md').read_text())
                count = sum(c[1] == 'upload' for c in store.calls)
                publish.main()
                self.assertEqual(sum(c[1] == 'upload' for c in store.calls), count)

    def test_mismatched_variants_and_failed_server_checksum_do_not_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out, config = reports(root)
            store = ReleaseStore()
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config), patch.object(publish, 'release_info', side_effect=lambda *a: store.release), patch.object(publish, 'gh', side_effect=store.gh), patch.dict(os.environ, GITHUB_REPOSITORY='owner/repo', GITHUB_SHA='b' * 40):
                path = out / 'source-ios18-report.json'
                original = path.read_text()
                changed = json.loads(original)
                changed['source_commit'] = 'f' * 40
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'different build inputs'):
                    publish.main()
                self.assertEqual(store.calls, [])
                path.write_text(original)
                store.corrupt_upload = True
                with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                    publish.main()
                self.assertTrue(store.release['draft'])

    def test_asset_uploads_are_concurrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / n for n in ('one.ipa', 'two.ipa')]
            for path in paths:
                path.write_bytes(path.name.encode())
            final = {'assets': [{'name': p.name, 'size': p.stat().st_size, 'state': 'uploaded',
                                 'digest': 'sha256:' + ci.sha256(p)} for p in paths]}
            barrier = threading.Barrier(2)
            with patch.object(publish, 'release_info', side_effect=[{'assets': []}, final]), patch.object(publish, 'gh', side_effect=lambda *a: barrier.wait(timeout=5)):
                publish.upload_verified('owner/repo', 'tag', paths)
