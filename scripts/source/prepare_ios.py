"""Produce reusable iOS inputs from ExeFS/RomFS and existing Legacy metadata."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci
from archive_input import extract
from input_config import download_file, input_identity, settings
from source.compatibility import generation_paths
from source.game_input import download, unpack
from source.prepared import FORMAT, GENERATED, verify_directory, verify_source, write
from source.publish import publish_assets
from source.romfs_input import create
from source.runtime_assets import HEAVY_DM

BASE = 'Payload/DmIOS.app/'


def run(*args, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, **kwargs).strip()


def plan():
    game = input_identity(*settings('GAME_ARCHIVE'))
    legacy = input_identity(*settings('LEGACY_METADATA'))
    digest = hashlib.sha256((game + '\0' + legacy + '\0' + os.environ['IOS_TOOLCHAIN']).encode())
    upstream = ci.ROOT / 'upstream'
    paths = generation_paths(upstream, None) | {'setup.sh', 'dm/tools/product.sh',
        'dm/ios/tools/gen_vault_table.py', 'dm/ios/tools/gen_hud_exp.py',
        'dm/tools/gen_bright_levels.py'}
    for name in sorted(paths):
        path = upstream / name
        if path.is_file():
            digest.update((name + '\0' + ci.sha256(path) + '\0').encode())
    digest.update(Path(__file__).read_bytes())
    digest.update((Path(__file__).parent / 'romfs_input.py').read_bytes())
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'input_revision={game}\nlegacy_revision={legacy}\n')
        output.write('prepared_key=ios-prepared-v1-' + digest.hexdigest() + '\n')


def cached():
    target = ci.ROOT / '.ios-prepared'
    info = verify_directory(target)
    if info.get('prepare_key') != os.environ['IOS_PREPARED_KEY']:
        raise ValueError('iOS preparation cache belongs to different inputs')
    verify_source(target, info, ci.ROOT / 'upstream', ci.CONFIG)
    return info


def verify():
    try:
        cached()
        reused = True
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        reused = False
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('reused=' + str(reused).lower() + '\n')


def legacy_metadata(workspace):
    url, digest = settings('LEGACY_METADATA')
    if input_identity(url, digest) != os.environ['LEGACY_REVISION']:
        raise ValueError('Legacy input changed after cache resolution')
    archive = ci.ROOT / '.ios-metadata-downloads/legacy.archive'
    marker = archive.with_suffix('.json')
    identity = input_identity(url, digest)
    try:
        saved = json.loads(marker.read_text())
        reused = (saved['identity'] == identity and saved['sha256'] == ci.sha256(archive)
                  and (not digest or saved['sha256'] == digest))
    except (OSError, ValueError, KeyError):
        reused = False
    if not reused:
        download_file(url, archive, digest or None, max_bytes=64 * 1024**2)
        write(marker, {'identity': identity, 'sha256': ci.sha256(archive)})
    target = workspace / 'legacy-metadata'
    shutil.rmtree(target, ignore_errors=True)
    extract(archive, target, max_bytes=64 * 1024**2, max_members=100)
    candidates = [p.parent for p in target.rglob('pack_index.tsv')
                  if (p.parent / 'pack.json').is_file()]
    if len(candidates) != 1:
        raise ValueError('Legacy metadata must contain one pack.json / pack_index.tsv pair')
    pack = json.loads((candidates[0] / 'pack.json').read_text())
    if (type(pack.get('size')) is not int or pack['size'] <= 0
            or not re.fullmatch('[a-f0-9]{64}', pack.get('sha256', ''))):
        raise ValueError('Legacy pack identity is incomplete')
    if (candidates[0] / 'pack_index.tsv').read_text().splitlines()[0].split('\t')[:2] != ['# pack', pack['name']]:
        raise ValueError('Legacy index does not match its pack descriptor')
    return candidates[0], pack


def write_payload(dm, workspace, python, destination, chain, product):
    stage = workspace / 'prepared-payload'
    shutil.rmtree(stage, ignore_errors=True)
    (stage / 'dm').mkdir(parents=True)
    rewritten = workspace / 'build/cache/dm/out' / (product + '-touch')
    subprocess.run([str(python), str(dm / 'tools/guest_dylib.py'), str(rewritten),
                    str(stage / 'Frameworks'), 'main', 'sdk'], check=True)
    (stage / 'Frameworks/Guest.framework/Guest').chmod(0o755)
    shutil.copytree(workspace / 'build/ios/dmsh_b' / ('b-' + chain), stage / 'msl' / ('b-' + chain))
    shutil.copy2(dm / 'gfx/nvn/c1_full.tsv', stage / 'dm/c1_full.tsv')
    for name in sorted(HEAVY_DM - {'c1_full.tsv'}):
        if (rewritten / name).is_file():
            shutil.copy2(rewritten / name, stage / 'dm' / name)
    (stage / 'dm/shell_info.txt').write_text(f'PRODUCT={product}\nCHAIN={chain}\nKIND=ios\n')
    manifest = [f'msl/b-{chain}/', 'Frameworks/Guest.framework/']
    manifest += sorted('dm/' + p.name for p in (stage / 'dm').iterdir())
    (stage / 'dm/shell_manifest.txt').write_text('\n'.join(manifest) + '\n')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                output.write(path, BASE + path.relative_to(stage).as_posix())


def prepare():
    root = ci.ROOT
    upstream = root / 'upstream'
    dm = upstream / 'dm'
    ios = dm / 'ios'
    workspace = root / '.ios-work'
    if input_identity(*settings('GAME_ARCHIVE')) != os.environ['GAME_REVISION']:
        raise ValueError('Game input changed after cache resolution')
    legacy, pack = legacy_metadata(workspace)
    game = unpack(workspace)
    environment = dict(os.environ, DM_ROOT=str(workspace))
    subprocess.run(['bash', str(upstream / 'setup.sh')], env=environment, check=True)
    python = workspace / 'build/cache/dm/venv/bin/python3'
    chain = run(python, dm / 'gfx/tools/dmsh_chain.py', '--header', env=environment)
    product = (dm / 'PRODUCT').read_text().strip()
    stage = root / '.ios-prepared.partial'
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    payload = stage / 'source-payload.zip'
    write_payload(dm, workspace, python, payload, chain, product)
    for name, script in zip(GENERATED, ('ios/tools/gen_vault_table.py', 'ios/tools/gen_hud_exp.py',
                                       'tools/gen_bright_levels.py')):
        destination = stage / 'generated' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('w') as output:
            subprocess.run([str(python), str(dm / script), str(game / 'RomFS')],
                           env=environment, stdout=output, check=True)
    (stage / 'legacy').mkdir()
    for name in ('pack.json', 'pack_index.tsv'):
        shutil.copy2(legacy / name, stage / 'legacy' / name)
    runtime_bytes = create(game.parent, stage / 'romfs-inputs.zip')
    record = run(python, '-c', 'import ios_metal; print(ios_metal.RECORD)',
                 cwd=ios / 'tools', env=environment)
    metal = {'metal': run('xcrun', '-sdk', 'iphoneos', 'metal', '--version').splitlines()[0],
             'xcode': run('xcodebuild', '-version'),
             'sdk': run('xcrun', '--sdk', 'iphoneos', '--show-sdk-build-version'),
             'compile_record': record}
    with zipfile.ZipFile(payload) as package:
        libraries = [n for n in package.namelist() if n.endswith('.metallib')]
        unique = len({hashlib.sha256(package.read(n)).hexdigest() for n in libraries})
    source_shaders = workspace / 'build/cache/metal_shader_cache' / ('b-' + chain)
    report = {'product': product, 'shader_chain': chain, 'shader_source_chain': chain,
              'payload_sha256': ci.sha256(payload), 'legacy_pack': pack,
              'shader_source_sha16': run(python, dm / 'tools/d2rm_pack.py', '--src-digest', source_shaders,
                                        env=environment),
              'metal_precompiled': True, 'metal_libraries': len(libraries),
              'metal_unique_libraries': unique, 'metal_toolchain': metal,
              'phone_shader_compilation_required': False}
    version = re.search(r'updated to\s+\*\*(\d+\.\d+\.\d+\.\d+)\*\*',
                        (upstream / 'docs/GAME_FILES.md').read_text())
    if version is None:
        raise ValueError('Cannot read the Switch resource version from upstream docs/GAME_FILES.md')
    game_version = game.parent.name
    resources = {'game_version': game_version, 'switch_version': version[1],
                 'files': [{'name': name, 'bytes': size}
                           for name, size in sorted(ci.resource_sizes().items())]}
    resources.update(archive_count=len(resources['files']),
                     resource_bytes=sum(item['bytes'] for item in resources['files']))
    write(stage / 'resource-manifest.json', resources)
    shutil.copy2(dm / 'gfx/tools/dmsh_chain.lock', stage / 'shader-chain.lock')
    info = {'format': FORMAT, 'schema_version': 2, 'game_version': game_version,
            'switch_version': version[1], 'nso_sha256': ci.CONFIG['nso_sha256'],
            'prepare_key': os.environ['IOS_PREPARED_KEY'],
            'original_input_identity': os.environ['GAME_REVISION'],
            'legacy_input_identity': os.environ['LEGACY_REVISION'],
            'producer': {'source_commit': ci.CONFIG['upstream_commit'],
                         'tools_commit': os.environ['TOOLS_COMMIT']},
            'payload': report,
            'runtime_input_bytes': runtime_bytes,
            'files': {p.relative_to(stage).as_posix(): {'bytes': p.stat().st_size, 'sha256': ci.sha256(p)}
                      for p in sorted(stage.rglob('*')) if p.is_file()}}
    write(stage / 'prepared.json', info)
    info = verify_directory(stage)
    verify_source(stage, info, upstream, ci.CONFIG)
    target = root / '.ios-prepared'
    shutil.rmtree(target, ignore_errors=True)
    stage.rename(target)


def export():
    info = cached()
    out = ci.ROOT / 'outputs'
    out.mkdir(exist_ok=True)
    package = out / 'D2RMobile-Prepared.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted((ci.ROOT / '.ios-prepared').rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(ci.ROOT / '.ios-prepared').as_posix())
    checksum = out / 'Prepared-SHA256SUMS.txt'
    checksum.write_text(ci.sha256(package) + '  ' + package.name + '\n')
    body = out / 'IOS-PREPARED-RELEASE.md'
    body.write_text('iOS prepared input / iOS 预处理包\n\n'
                    f'Source / 源码：`{info["producer"]["source_commit"]}`\n\n'
                    'Set this ZIP’s URL and SHA-256 in the prepared_input workflow parameter. / '
                    '将此 ZIP 的地址和校验值填入运行参数 prepared_input。\n')
    publish_assets(os.environ['GITHUB_REPOSITORY'], info['prepare_key'], 'iOS prepared input', body, [package, checksum])


if __name__ == '__main__':
    try:
        {'plan': plan, 'verify': verify, 'download': download,
         'prepare': prepare, 'export': export}[sys.argv[1]]()
    except Exception as exc:
        print('::error::' + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
