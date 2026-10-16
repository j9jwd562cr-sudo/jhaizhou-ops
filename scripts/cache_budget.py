"""Save build caches only when repository cache space permits."""
import json
import os
from pathlib import Path
import subprocess
import sys

LIMIT = 10 * 1024**3
RESERVE = 256 * 1024**2


def can_save(used, size, limit=LIMIT, reserve=RESERVE):
    return used is not None and size > 0 and used + size + reserve <= limit


def main():
    try:
        result = subprocess.check_output(['gh', 'api', 'repos/' + os.environ['GITHUB_REPOSITORY'] +
                                          '/actions/cache/usage'], text=True, stderr=subprocess.DEVNULL)
        used = json.loads(result)['active_caches_size_in_bytes']
    except (subprocess.CalledProcessError, ValueError, KeyError):
        used = None
    path = sys.argv[1] if len(sys.argv) > 1 else '.native-module-cache'
    output = sys.argv[2] if len(sys.argv) > 2 else 'native'
    files = [p for p in Path(path).rglob('*') if p.is_file()]
    size = sum(p.stat().st_size + 4096 for p in files) + 16 * 1024**2 if files else 0
    enabled = can_save(used, size)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
        out.write(output + '=' + str(enabled).lower() + '\n')
    if not size:
        reason = 'no files to cache'
    elif used is None:
        reason = 'repository cache usage is unavailable'
    else:
        reason = (f'{used / 1024**3:.2f} GiB used + {size / 1024**2:.1f} MiB estimated cache'
                  f' + {RESERVE / 1024**2:.0f} MiB reserve; limit {LIMIT / 1024**3:.0f} GiB')
    print(f'Save cache {path}: {enabled} ({reason})')


if __name__ == '__main__':
    main()
