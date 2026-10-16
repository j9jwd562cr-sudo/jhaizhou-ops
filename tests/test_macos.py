import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
import macos
from source import game_input
from fixtures import CHAIN, runtime_input, source


def runtime_files(cache):
    runtime_input(cache / 'romfs-inputs.zip')
    for name in ('dm_vault_tab.h', 'dm_hud_exp.h', 'dm_bright_levels.inc'):
        path = cache / 'generated' / name
        path.parent.mkdir(exist_ok=True); path.write_text('fixture table\n')
    for module in ('main', 'sdk', 'subsdk0'):
        for suffix in ('patched.bin', 'fixups.tsv', 'gates.tsv'):
            path = cache / 'product' / (module + '.' + suffix)
            path.parent.mkdir(exist_ok=True); path.write_bytes(b'fixture program')
    (cache / 'shader-chain.lock').write_text('fixture metadata')
    (cache / 'c1_full.tsv').write_text('# chain b-' + CHAIN + ' fixture\n')
    shaders = cache / 'msl' / ('b-' + CHAIN)
    shaders.mkdir(parents=True)
    (shaders / 'shader_b.metallib').write_bytes(b'MTLBfixture')
    (shaders / 'shader_b.json').write_text(json.dumps({'key': 'shader_b', 'chain': CHAIN,
                                                     'entry': 'main0', 'bindings': []}))


class MacShaderTests(unittest.TestCase):
    def test_retarget_copies_metadata_without_expanding_filenames_into_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'shader inputs'; source.mkdir()
            output = root / 'retargeted'
            for number in range(2500):
                (source / (str(number) + 'x' * 120 + '.json')).write_text('{}')
            script = root / 'retarget.sh'
            script.write_text('set -u\nS="$INPUT"; D="$2"; mkdir -p "$D"; cp -c "$S"/*.json "$D"/\n')
            macos.retarget_shaders(script, '15.0', output, dict(os.environ, INPUT=str(source)))
            self.assertEqual({p.name for p in source.iterdir()}, {p.name for p in output.iterdir()})

    def test_missing_reflection_invalid_json_and_wrong_chain_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'shader_b.metallib').write_bytes(b'MTLBfixture')
            with self.assertRaisesRegex(ValueError, 'reflection JSON set is incomplete'):
                macos.verify_shaders(directory, CHAIN, 1)
            reflection = directory / 'shader_b.json'
            data = {'key': 'shader_b', 'chain': CHAIN, 'entry': 'main0', 'bindings': []}
            reflection.write_text(json.dumps(data))
            self.assertEqual(macos.verify_shaders(directory, CHAIN, 1), 1)
            reflection.write_text(json.dumps(dict(data, chain='wrong')))
            with self.assertRaisesRegex(ValueError, 'Invalid Mac shader reflection'):
                macos.verify_shaders(directory, CHAIN, 1)
            reflection.write_text(json.dumps(dict(data, invalid=float('nan'))))
            with self.assertRaisesRegex(ValueError, 'Non-finite'):
                macos.verify_shaders(directory, CHAIN, 1)

    def test_final_app_checks_program_tables_shaders_and_generated_overlays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root / 'prepared'; prepared.mkdir(); runtime_files(prepared)
            info = {'shader_chain': CHAIN, 'metal_libraries': 1,
                    'files': {p.relative_to(prepared).as_posix(): ci.sha256(p)
                              for p in prepared.rglob('*') if p.is_file()}}
            app = root / 'App.app'; resources = app / 'Contents/Resources'
            import shutil
            for directory in ('product', 'msl'):
                shutil.copytree(prepared / directory, resources / directory)
            dm = root / 'dm'; overlay = root / 'overlay'
            overlay.mkdir(); (overlay / 'touch_strings.tsv').write_text('current interface')
            shutil.copytree(overlay, resources / 'romfs_overlay')
            for name in ('shader_map.tsv', 'pso_manifest.tsv', 'c1_full.tsv'):
                path = dm / 'gfx/nvn' / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('source runtime table'); shutil.copyfile(path, resources / name)
            checked = macos.verify_app_resources(app, dm, info, overlay)
            self.assertIn('product/sdk.gates.tsv', checked)
            (resources / 'msl' / ('b-' + CHAIN) / 'shader_b.json').unlink()
            with self.assertRaisesRegex(ValueError, 'runtime resource missing'):
                macos.verify_app_resources(app, dm, info, overlay)


class MacBuildTests(unittest.TestCase):
    def test_native_changes_reuse_generation_but_changed_generators_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, _, _, _ = source(Path(temporary))
            original = macos.generation_key(upstream, 'input', 'xcode')
            (upstream / 'dm/runtime.c').write_text('new native code\n')
            self.assertEqual(macos.generation_key(upstream, 'input', 'xcode'), original)
            (upstream / 'dm/tools/helper.py').write_text('changed_generation = True\n')
            self.assertNotEqual(macos.generation_key(upstream, 'input', 'xcode'), original)
            self.assertNotEqual(macos.generation_key(upstream, 'other-input', 'xcode'), original)

    def test_existing_archive_cache_is_verified_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / '.game-downloads/game-input.archive'
            archive.parent.mkdir()
            archive.write_bytes(b'existing ExeFS/RomFS archive')
            digest = ci.sha256(archive)
            with patch.object(ci, 'ROOT', root), patch.object(game_input, 'settings', return_value=('https://storage.invalid/game.zip', digest)), patch.object(game_input, 'download_file') as download:
                macos.download()
                download.assert_not_called()
            self.assertEqual(archive.read_bytes(), b'existing ExeFS/RomFS archive')

    def test_prepared_cache_integrity_and_macos_executable_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / '.macos-prepared'
            cache.mkdir()
            payload = cache / 'program'
            payload.write_bytes(b'prepared')
            runtime_files(cache)
            (cache / 'manifest.json').write_text(json.dumps({'format': 'd2r-mobile-macos-prepared',
                'schema_version': 2, 'key': 'cache-key',
                'shader_chain': CHAIN, 'metal_libraries': 1,
                'files': {p.relative_to(cache).as_posix(): ci.sha256(p)
                          for p in cache.rglob('*') if p.is_file()}}))
            with patch.object(ci, 'ROOT', root), patch.dict(os.environ, MACOS_PREPARED_KEY='cache-key'):
                self.assertEqual(macos.load_prepared()['key'], 'cache-key')
                payload.write_bytes(b'damaged')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    macos.load_prepared()
        native = struct.pack('<8I6I', 0xfeedfacf, 0x0100000c, 0, 2, 1, 24, 0, 0,
                             0x32, 24, 1, 15 << 16, 26 << 16, 0)
        self.assertEqual(ci.macho_minimum_os(native, platform=1), (15, 0, 0))
        with self.assertRaises(ValueError):
            ci.macho_minimum_os(native)

    def test_prepared_archive_allows_native_changes_and_rejects_generation_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, commit, _, _ = source(root)
            package = root / 'package'
            package.mkdir()
            (package / 'program').write_bytes(b'Mac prepared program')
            runtime_files(package)
            config = {'nso_sha256': {'main': 'a' * 64}}
            info = {'format': 'd2r-mobile-macos-prepared', 'schema_version': 2,
                    'source_commit': commit, 'nso_sha256': config['nso_sha256'],
                    'shader_chain': CHAIN, 'metal_libraries': 1,
                    'input_revision': 'raw-archive', 'toolchain': 'producer-xcode',
                    'key': macos.generation_key(upstream, 'raw-archive', 'producer-xcode'),
                    'files': {p.relative_to(package).as_posix(): ci.sha256(p)
                              for p in package.rglob('*') if p.is_file()}}
            (package / 'manifest.json').write_text(json.dumps(info))
            archive = root / '.macos-downloads/prepared-input.archive'
            archive.parent.mkdir()
            with zipfile.ZipFile(archive, 'w') as z:
                for path in package.rglob('*'):
                    if path.is_file():
                        z.write(path, path.relative_to(package))
            before = archive.read_bytes()
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config):
                (upstream / 'dm/runtime.c').write_text('updated native runtime\n')
                macos.import_prepared()
                self.assertEqual((root / '.macos-prepared/program').read_bytes(), b'Mac prepared program')
                (upstream / 'dm/ios/tools/gen_assets.py').write_text('updated_interface = True\n')
                macos.import_prepared()
                (upstream / 'dm/tools/helper.py').write_text('changed_generation = True\n')
                with self.assertRaisesRegex(ValueError, 'changed generation inputs'):
                    macos.import_prepared()
            self.assertEqual(archive.read_bytes(), before)

    def test_mac_release_checks_both_packages_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / 'outputs'
            out.mkdir()
            config = {'upstream_commit': 'a' * 40, 'build_label': 'v1.2.3',
                      'build_number': '5.2', 'release_tag': 'v1.2.3-build.5.2'}
            report = {'source_commit': 'a' * 40, 'portable': True,
                      'native_built_from_source': True, 'release_tag': config['release_tag'] + '-macos', 'packages': []}
            for minimum in ('26.0', '15.0'):
                name = f'D2RMobile-{config["release_tag"]}-macos{minimum.split(".")[0]}.zip'
                body = name.encode()
                (out / name).write_bytes(body)
                report['packages'].append({'name': name, 'minimum_os': minimum,
                                           'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)})
            (out / 'macos-build-report.json').write_text(json.dumps(report))
            (out / 'Mac-SHA256SUMS.txt').write_text('fixture')
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config), patch.dict(os.environ, GITHUB_REPOSITORY='owner/private'), patch.object(macos, 'publish_assets') as publish:
                macos.publish()
                self.assertEqual(len(publish.call_args.args[-1]), 4)
                self.assertIn('## 简体中文', (out / 'MAC-RELEASE.md').read_text())
                publish.reset_mock()
                (out / report['packages'][0]['name']).write_bytes(b'damaged')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    macos.publish()
                publish.assert_not_called()
