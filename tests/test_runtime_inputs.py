import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from source.compatibility import verify_generation
from source.romfs_input import create, restore
from fixtures import source


def key(offset):
    return (bytes(11) + offset.to_bytes(5, 'big')).hex()


class RuntimeInputTests(unittest.TestCase):
    def test_small_input_preserves_original_addresses_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'original.zip'
            rows = []
            for path, offset, size in (('data/global/ui/layouts/settings.json', 64, 8),
                                       ('data/hd/world/huge.texture', 80, 16),
                                       ('data/global/excel/base/inventory.bin', 200, 8)):
                rows.append(f'1|0|0|0|unused|{size}|{key(offset)}|{size}|n|data:{path}|0')
            manifest = '\n'.join(rows) + '\n'
            config = f'vfs-1 = unused {key(128)}\nvfs-1-size = 8 8\nvfs-root = unused {key(144)}\nvfs-root-size = 4 4\n'
            data = bytearray(1024 * 1024)
            data[64:72] = b'original'; data[80:96] = b'not needed large'; data[128:136] = b'tvfs-one'; data[144:148] = b'root'; data[200:208] = b'invtable'
            with zipfile.ZipFile(original, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('Update/RomFS/data/build_manifest.psv', manifest)
                archive.writestr('Update/RomFS/data/.build.config', config)
                archive.writestr('Update/RomFS/data/00-00000000.data', data)
            package = root / 'inputs.zip'
            self.assertEqual(create(original, package), 28)
            romfs = restore(package, root / 'RomFS')
            self.assertEqual((romfs / 'data/build_manifest.psv').read_text(), manifest)
            self.assertEqual((romfs / 'data/.build.config').read_text(), config)
            with (romfs / 'data/00-00000000.data').open('rb') as stream:
                stream.seek(64); self.assertEqual(stream.read(8), b'original')
                stream.seek(80); self.assertEqual(stream.read(16), bytes(16))
                stream.seek(128); self.assertEqual(stream.read(8), b'tvfs-one')
                stream.seek(144); self.assertEqual(stream.read(4), b'root')
                stream.seek(200); self.assertEqual(stream.read(8), b'invtable')
            self.assertLess(package.stat().st_size, 2048)
            self.assertEqual((romfs / 'data/00-00000000.data').stat().st_size, len(data))

    def test_invalid_range_index_cannot_write_outside_resource_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('ranges.json', json.dumps({'sizes': {'../escape': 8}, 'ranges': {}}))
            with self.assertRaisesRegex(ValueError, 'Invalid original resource'):
                restore(archive, root / 'RomFS')
            self.assertFalse((root / 'escape').exists())

    def test_ui_updates_reuse_heavy_outputs_but_game_rewrite_changes_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, commit, _, _ = source(Path(temporary))
            path = upstream / 'dm/ios/tools/touch_layout.py'
            path.write_text('new_quality_setting = True\n')
            verify_generation(upstream, commit)
            for name in ('dm/tools/helper.py', 'dm/gfx/tools/kernel.py', 'dm/data/bootlist_fixture.tsv'):
                with self.subTest(name=name):
                    path = upstream / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    original = path.read_bytes() if path.is_file() else None
                    path.write_text('changed_program_rewrite = True\n')
                    with self.assertRaisesRegex(ValueError, 'updated prepared package'):
                        verify_generation(upstream, commit)
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)


class InterfaceCacheTests(unittest.TestCase):
    def test_native_changes_keep_cache_but_ui_text_and_inputs_invalidate_it(self):
        from source.runtime_assets import generation_key
        from fixtures import git
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, _, _, _ = source(root)
            archive = root / '.prepared-downloads/prepared-input.archive'
            archive.parent.mkdir(); archive.write_bytes(b'original prepared input')
            for name in ('scripts/source/runtime_assets.py', 'scripts/source/compatibility.py',
                         'scripts/source/romfs_input.py', 'scripts/png_backend.sh'):
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text('fixture tools\n')
            text = upstream / 'dm/i18n/ui_text.tsv'
            text.parent.mkdir(); text.write_text('original interface text\n')
            git(upstream, 'add', '.')
            first = generation_key('ios', root)
            (upstream / 'dm/runtime.c').write_text('new native implementation\n')
            self.assertEqual(generation_key('ios', root), first)
            (upstream / 'dm/ios/tools/project_free.py').write_text('native_project = True\n')
            self.assertEqual(generation_key('ios', root), first)
            helper = upstream / 'dm/ios/tools/interface_helper.py'
            helper.write_text('layout = 1\n')
            generator = upstream / 'dm/ios/tools/gen_assets.py'
            original = generator.read_text()
            generator.write_text(original + '\n__import__("interface_helper")\n')
            changed_generator = generation_key('ios', root)
            self.assertNotEqual(changed_generator, first)
            helper.write_text('layout = 2\n')
            self.assertNotEqual(generation_key('ios', root), changed_generator)
            generator.write_text(original)
            template = upstream / 'dm/ios/tools/layout.json'
            template.write_text('{"width": 100}\n')
            changed_template = generation_key('ios', root)
            self.assertNotEqual(changed_template, first)
            template.write_text('{"width": 200}\n')
            self.assertNotEqual(generation_key('ios', root), changed_template)
            before_text = generation_key('ios', root)
            text.write_text('new interface text\n')
            second = generation_key('ios', root)
            self.assertNotEqual(second, before_text)
            archive.write_bytes(b'updated prepared input')
            self.assertNotEqual(generation_key('ios', root), second)

    def test_cached_generated_files_restore_without_replacing_native_sources(self):
        from unittest.mock import patch
        import ci
        from source.runtime_assets import cached_interface, save_interface
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dm = root / 'upstream/dm'; workspace = root / '.runtime-inputs'
            generated = dm / 'ios/Sources/dm_touch_native_art.c'
            generated.parent.mkdir(parents=True); generated.write_text('generated skill index\n')
            native = dm / 'ios/Sources/application.m'; native.write_text('current native code\n')
            overlay = workspace / 'romfs_overlay/data/new.data'
            overlay.parent.mkdir(parents=True); overlay.write_bytes(b'generated overlay')
            with patch.object(ci, 'ROOT', root):
                save_interface('source-key', dm, workspace, 100.0)
                generated.unlink(); overlay.unlink()
                self.assertFalse(cached_interface('different-source', dm, workspace))
                self.assertTrue(cached_interface('source-key', dm, workspace))
                self.assertEqual(generated.read_text(), 'generated skill index\n')
                self.assertEqual(overlay.read_bytes(), b'generated overlay')
                self.assertEqual(native.read_text(), 'current native code\n')
                (root / '.interface-cache/interface.zip').write_bytes(b'corrupt')
                self.assertFalse(cached_interface('source-key', dm, workspace))
