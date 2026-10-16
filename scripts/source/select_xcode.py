"""Select iOS 26.4+ SDK from metadata, then verify the chosen Xcode once."""
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess


def candidates(applications=Path('/Applications')):
    found = []
    for app in applications.glob('Xcode*.app'):
        developer = app / 'Contents/Developer'
        for sdk in (developer / 'Platforms/iPhoneOS.platform/Developer/SDKs').glob('*.sdk'):
            try:
                settings = plistlib.loads((sdk / 'SDKSettings.plist').read_bytes())
                version = tuple(map(int, settings['Version'].split('.')))
                xcode = plistlib.loads((app / 'Contents/version.plist').read_bytes())
                if version >= (26, 4):
                    found.append((version, str(xcode['CFBundleShortVersionString']), developer,
                                  {'sdk': settings, 'xcode': xcode}))
            except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
                continue
    return sorted(found, key=lambda item: (item[0], tuple(map(int, item[1].split('.')))), reverse=True)


def select(applications=Path('/Applications')):
    attempted = set()
    for version, _, developer, metadata in candidates(applications):
        if developer in attempted:
            continue
        attempted.add(developer)
        try:
            actual = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'],
                env=dict(os.environ, DEVELOPER_DIR=str(developer)), text=True).strip()
            if tuple(map(int, actual.split('.'))) != version:
                continue
        except (subprocess.CalledProcessError, ValueError):
            continue
        identity = hashlib.sha256(json.dumps(metadata, sort_keys=True, default=str).encode()).hexdigest()
        return developer, actual, identity
    raise ValueError('Runner has no usable iOS 26.4+ SDK')


def main():
    if platform.system() != 'Darwin' or int(platform.mac_ver()[0].split('.')[0]) < 26:
        raise SystemExit('Source builds require macOS 26 or later')
    developer, sdk, identity = select()
    with open(os.environ['GITHUB_ENV'], 'a') as out:
        out.write('DEVELOPER_DIR=' + str(developer) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write('toolchain=' + identity + '\n')
    print('Selected macOS', platform.mac_ver()[0], 'iOS SDK', sdk, developer)


if __name__ == '__main__':
    main()
