"""Select the latest official source release unless the caller requests a ref."""
import json
import os
import subprocess


def resolve_ref(repository, requested=''):
    requested = requested.strip()
    if requested:
        if '\n' in requested or '\r' in requested:
            raise ValueError('Source ref must be a single line')
        return requested
    release = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{repository}/releases/latest'], text=True))
    tag = release.get('tag_name')
    if (release.get('draft') is not False or release.get('prerelease') is not False
            or not isinstance(tag, str) or not tag or '\n' in tag or '\r' in tag):
        raise ValueError('Cannot select the latest official release')
    return 'refs/tags/' + tag


if __name__ == '__main__':
    ref = resolve_ref(os.environ['SOURCE_REPOSITORY'], os.environ.get('SOURCE_REF', ''))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write('ref=' + ref + '\n')
    print('Selected source ref:', ref)
