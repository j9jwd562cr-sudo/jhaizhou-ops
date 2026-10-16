"""Record the selected source, expected programs and per-run app identity."""
import json
import os
from pathlib import Path
import re
import subprocess


def expected_programs(upstream):
    """Read the selected source's checksum table without executing its setup script."""
    source = (upstream / 'setup.sh').read_text()
    tables = re.findall(r"(?ms)^done[ \t]+<<-?[ \t]*['\"]?SUMS['\"]?[ \t]*\n(.*?)^SUMS[ \t]*$", source)
    if len(tables) != 1:
        raise ValueError('Cannot read the upstream ExeFS checksum table in setup.sh')
    checksums = {}
    for line in tables[0].splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'\s*([a-fA-F0-9]{64})\s+(main|sdk|subsdk0)\s*', line)
        if not match or match[2] in checksums:
            raise ValueError('Invalid upstream ExeFS checksum table in setup.sh')
        checksums[match[2]] = match[1].lower()
    if set(checksums) != {'main', 'sdk', 'subsdk0'}:
        raise ValueError('Incomplete upstream ExeFS checksum table in setup.sh')
    return checksums


def source_identity(upstream):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(upstream), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    commit = git('rev-parse', 'HEAD')
    label = 'src-' + commit[:12]
    try:
        description = git('describe', '--tags', '--match', 'v[0-9]*', '--long',
                          '--abbrev=12', 'HEAD')
    except subprocess.CalledProcessError:
        description = ''
    match = re.fullmatch(r'(v\d+\.\d+(?:\.\d+)?)-(\d+)-g[0-9a-f]+', description)
    if match:
        label = match[1]
        if int(match[2]):
            label += '-src-' + commit[:12]
    return commit, label


def configure(root):
    upstream = root / 'upstream'
    commit, label = source_identity(upstream)
    config = {'upstream_commit': commit, 'nso_sha256': expected_programs(upstream)}
    config['build_label'] = label
    version = re.match(r'v(\d+\.\d+(?:\.\d+)?)', label)
    config['app_version'] = version[1] if version else '0.0.0'
    number = os.environ.get('GITHUB_RUN_NUMBER', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if number and (not re.fullmatch(r'[1-9]\d*', number) or
                   not re.fullmatch(r'[1-9]\d*', attempt)):
        raise ValueError('Invalid GitHub build number or attempt')
    config['build_number'] = number + '.' + attempt if number else '1'
    config['release_tag'] = label + '-build.' + config['build_number'] if number else label
    output = root / 'outputs/build-context.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + '\n')
    print('Selected source:', commit, 'build:', label)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('source_commit='+commit+'\n')
    return config


if __name__ == '__main__':
    configure(Path(__file__).resolve().parents[2])
