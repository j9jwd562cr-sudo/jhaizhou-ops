import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import ci
from archive_input import extract
from source import prepared, prepare_ios
from source.metal import verify_payload
from source.configure import configure
from fixtures import BASE, CHAIN, git, package


class PreparedTests(unittest.TestCase):
    def test_existing_package_uses_selected_native_sources_and_fresh_build_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, directory, info = package(root)
            (upstream / 'dm/runtime.c').write_text('updated runtime\n')
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-m', 'runtime update')
            with patch.dict(os.environ, GITHUB_RUN_NUMBER='7', GITHUB_RUN_ATTEMPT='2', SOURCE_REPOSITORY='owner/fork', INPUT_REVISION='f' * 64):
                config = configure(root)
                with patch.object(ci, 'ROOT', root), patch.object(ci, 'CONFIG', config), patch('source.runtime_assets.generation_key', return_value='fixture-interface'):
                    prepared.prepare(root)
            report = json.loads((root / 'outputs/source-input-report.json').read_text())
            self.assertEqual(report['source_commit'], git(upstream, 'rev-parse', 'HEAD'))
            self.assertEqual(report['prepared_from_source'], info['producer']['source_commit'])
            self.assertEqual(json.loads((root / 'outputs/build-context.json').read_text())['build_number'], '7.2')
            with tarfile.open(root / 'outputs/native-inputs.tar.gz') as archive:
                self.assertEqual(archive.extractfile('upstream/dm/runtime.c').read(), b'updated runtime\n')
            with zipfile.ZipFile(root / 'outputs/source-payload.zip') as archive:
                self.assertEqual(archive.read('Payload/DmIOS.app/hud_v4_ui/ui.json'), b'original')
            self.assertIsInstance(report['runtime_generation_seconds'], (int, float))

    def test_setup_outputs_produce_a_verified_game_and_metal_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, directory, info = package(root)
            dm = upstream / 'dm'
            workspace = root / 'workspace'
            rewritten = workspace / 'build/cache/dm/out/fixture-product-touch'
            rewritten.mkdir(parents=True)
            (rewritten / 'main.fixups.tsv').write_text('fixture main loader table\n')
            (dm / 'gfx/nvn/c1_full.tsv').write_text(f'# chain b-{CHAIN} fixture\n')
            (dm / 'tools/guest_dylib.py').write_text(
                "from pathlib import Path\nimport sys\np=Path(sys.argv[2])/'Guest.framework'\n"
                "p.mkdir(parents=True)\n(p/'Guest').write_bytes(bytes.fromhex('cffaedfe0c000001'))\n"
                "(p/'Info.plist').write_text('fixture framework metadata')\n")
            libraries = workspace / 'build/ios/dmsh_b' / ('b-' + CHAIN)
            libraries.mkdir(parents=True)
            (libraries / 'one_b.metallib').write_bytes(b'MTLBfixture shader')
            (libraries / 'one_b.json').write_text(json.dumps({'chain': CHAIN, 'chain_metal': 'fixture-metal'}))
            payload = root / 'payload.zip'
            prepare_ios.write_payload(dm, workspace, sys.executable, payload, CHAIN, 'fixture-product')
            verify_payload(payload, info['payload'])
            with zipfile.ZipFile(payload) as archive:
                self.assertEqual(archive.read(BASE + 'dm/main.fixups.tsv'), b'fixture main loader table\n')
                self.assertEqual(archive.read(BASE + 'Frameworks/Guest.framework/Info.plist'), b'fixture framework metadata')
                self.assertEqual(archive.getinfo(BASE + 'Frameworks/Guest.framework/Guest').external_attr >> 16 & 0o777, 0o755)

    def test_modified_payload_or_wrong_program_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, directory, info = package(root)
            with patch.object(ci, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'game programs'):
                    prepared.verify_source(directory, info, upstream, {'nso_sha256': {'main': '0' * 64}})
            (directory / 'source-payload.zip').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                prepared.verify_directory(directory)

    def test_zip_and_tar_paths_and_links_are_checked_before_extraction(self):
        for kind, name, link in (('zip', '../escape', False), ('zip', 'link', True),
                                 ('tar', '/escape', False), ('tar', 'link', True)):
            with self.subTest(kind=kind, name=name, link=link), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / 'input'
                if kind == 'zip':
                    with zipfile.ZipFile(path, 'w') as z:
                        item = zipfile.ZipInfo(name)
                        if link:
                            item.external_attr = 0o120777 << 16
                        z.writestr(item, b'data')
                else:
                    with tarfile.open(path, 'w') as tf:
                        item = tarfile.TarInfo(name)
                        if link:
                            item.type = tarfile.SYMTYPE
                            item.linkname = '../escape'
                        else:
                            item.size = 4
                        tf.addfile(item, None if link else io.BytesIO(b'data'))
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    extract(path, root / 'expanded')
                self.assertEqual(path.read_bytes(), before)

    def test_download_cache_reuses_verified_bytes_and_recovers_from_corruption(self):
        content = b'prepared archive'
        def fetch(url, destination, *args, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        with tempfile.TemporaryDirectory() as temporary, patch.object(prepared, 'settings', return_value=('https://storage.invalid/input.zip', None)), patch.object(prepared, 'download_file', side_effect=fetch) as download:
            root = Path(temporary)
            prepared.download(root)
            prepared.download(root)
            self.assertEqual(download.call_count, 1)
            (root / '.prepared-downloads/prepared-input.archive').write_bytes(b'corrupt')
            prepared.download(root)
            self.assertEqual(download.call_count, 2)

    def test_macho_platform_and_load_commands(self):
        def binary(platform=2):
            return struct.pack('<8I6I', 0xfeedfacf, 0x0100000c, 0, 2, 1, 24, 0, 0, 0x32, 24, platform, 18 << 16, 18 << 16, 0)
        self.assertEqual(ci.macho_minimum_os(binary()), (18, 0, 0))
        for data in (binary(7), binary()[:40], bytes(56)):
            with self.assertRaises(ValueError):
                ci.macho_minimum_os(data)
