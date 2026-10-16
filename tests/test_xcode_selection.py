import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from source.select_xcode import select


def application(root, name, version, build='build-one'):
    app = root / name
    sdk = app / 'Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk'
    sdk.mkdir(parents=True)
    (sdk / 'SDKSettings.plist').write_bytes(plistlib.dumps({'Version': version}))
    (app / 'Contents/version.plist').write_bytes(plistlib.dumps({
        'CFBundleShortVersionString': version, 'ProductBuildVersion': build}))
    return app / 'Contents/Developer'


class XcodeSelectionTests(unittest.TestCase):
    def test_metadata_selects_latest_sdk_with_only_one_xcrun_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            application(root, 'Xcode-old.app', '26.2')
            application(root, 'Xcode-26.4.app', '26.4')
            latest = application(root, 'Xcode-latest.app', '26.5')
            with patch('source.select_xcode.subprocess.check_output', return_value='26.5\n') as probe:
                developer, sdk, identity = select(root)
            self.assertEqual((developer, sdk), (latest, '26.5'))
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(len(identity), 64)

    def test_unusable_latest_sdk_falls_back_to_verified_compatible_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fallback = application(root, 'Xcode-26.4.app', '26.4')
            application(root, 'Xcode-latest.app', '26.5')
            with patch('source.select_xcode.subprocess.check_output', side_effect=[
                    subprocess.CalledProcessError(1, 'xcrun'), '26.4']):
                self.assertEqual(select(root)[0], fallback)
            with patch('source.select_xcode.subprocess.check_output', return_value='26.3'):
                with self.assertRaisesRegex(ValueError, 'no usable'):
                    select(root)

    def test_changed_xcode_build_invalidates_module_cache_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            developer = application(root, 'Xcode.app', '26.4')
            with patch('source.select_xcode.subprocess.check_output', return_value='26.4'):
                first = select(root)[2]
                (developer.parent / 'version.plist').write_bytes(plistlib.dumps({
                    'CFBundleShortVersionString': '26.4', 'ProductBuildVersion': 'build-two'}))
                self.assertNotEqual(first, select(root)[2])


if __name__ == '__main__':
    unittest.main()
