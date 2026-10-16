"""Generate interface resources from the selected source and small original inputs."""
import os
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source.romfs_input import restore
from archive_input import extract
import ci
from source.compatibility import git, python_dependencies

BASE = 'Payload/DmIOS.app/'
HEAVY_DM = {'c1_full.tsv', 'bootlist.tsv', 'main.cuts.tsv',
            'main.fixups.tsv', 'main.gates.tsv', 'sdk.fixups.tsv', 'sdk.gates.tsv'}


def heavy_member(name):
    relative = name.removeprefix(BASE)
    return relative.startswith(('msl/', 'Frameworks/Guest.framework/')) or relative in {'dm/' + n for n in HEAVY_DM}


UI_SCRIPTS = ('dm/ios/tools/gen_assets.py', 'dm/ios/tools/gen_romfs_overlay.py',
              'dm/ios/tools/gen_vault_table.py', 'dm/ios/tools/gen_hud_exp.py',
              'dm/tools/casc_overlay.py', 'dm/tools/gen_bright_levels.py')
UI_INPUTS = ('dm/ios/docs', 'dm/ios/NativeHUD', 'dm/i18n', 'dm/data',
             'dm/gfx/tex', 'dm/gfx/nvn/include', 'dm/gfx/nvn/nvn_tex.c', 'dm/gfx/nvn/nvn_tex.h',
             'dm/ios/Sources/TouchArt/native_art_manifest.json', 'dm/host/dm_kills.c')


def generation_key(kind, root=ci.ROOT):
    archive = root / ('.macos-downloads' if kind == 'macos' else '.prepared-downloads') / 'prepared-input.archive'
    digest = hashlib.sha256((kind + platform.machine() + ci.sha256(archive)).encode())
    for package in ('Pillow', 'numpy'):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = 'missing'
        digest.update((package + version).encode())
    upstream = root / 'upstream'
    names = {n for n in git(upstream, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
             if n and (upstream / n).is_file()}
    dependencies = python_dependencies(names, UI_SCRIPTS, lambda n: (upstream / n).read_text())
    # Keep generator data (JSON, templates, artwork) even when its filename is assembled at runtime.
    dependencies.update(n for n in names if any(n == p or n.startswith(p + '/') for p in UI_INPUTS)
                        or n.startswith('dm/ios/tools/') and not n.endswith('.py'))
    for name in sorted(dependencies):
        digest.update((name + '\0').encode() + (upstream / name).read_bytes())
    for name in ('scripts/source/runtime_assets.py', 'scripts/source/compatibility.py',
                 'scripts/source/romfs_input.py', 'scripts/png_backend.sh'):
        digest.update((root / name).read_bytes())
    return 'interface-v1-' + kind + '-' + digest.hexdigest()


def cache_paths(dm, workspace):
    return {'assets': dm / 'ios/Gen/assets', 'art': dm / 'ios/Sources/TouchArt',
            'native': dm / 'ios/Sources', 'tables': dm / 'host',
            **{name: workspace / name for name in ('romfs_overlay', 'romfs_overlay_variants',
                                                  'romfs_overlay_astc', 'romfs_overlay_astc_variants')}}


def cached_interface(key, dm, workspace):
    cache = ci.ROOT / '.interface-cache'
    temporary = cache / 'unpacked'
    try:
        info = json.loads((cache / 'receipt.json').read_text())
        if info['key'] != key or ci.sha256(cache / 'interface.zip') != info['sha256']:
            return False
        extract(cache / 'interface.zip', temporary, max_members=10000, max_bytes=512 * 1024**2)
        for name, target in cache_paths(dm, workspace).items():
            source = temporary / name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
                for path in source.rglob('*'):
                    if path.is_file():
                        os.utime(target / path.relative_to(source), None)
        print('Reused interface resources for the selected source', flush=True)
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def save_interface(key, dm, workspace, seconds):
    cache = ci.ROOT / '.interface-cache'
    cache.mkdir(exist_ok=True)
    with zipfile.ZipFile(cache / 'interface.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as output:
        for name, directory in cache_paths(dm, workspace).items():
            for path in sorted(directory.rglob('*')) if directory.is_dir() else ():
                if not path.is_file():
                    continue
                relative = path.relative_to(directory).as_posix()
                if (name == 'native' and relative != 'dm_touch_native_art.c'
                        or name == 'art' and not path.name.startswith('dm_native_')
                        or name == 'tables' and relative not in ('dm_vault_tab.h', 'dm_hud_exp.h', 'dm_bright_levels.inc')):
                    continue
                output.write(path, name + '/' + relative)
    (cache / 'receipt.json').write_text(json.dumps({'key': key, 'sha256': ci.sha256(cache / 'interface.zip'),
                                                  'generation_seconds': seconds}) + '\n')


def generate(dm, inputs, workspace, *, macos=False, legacy=None, headers=None, baseline=None):
    started = time.monotonic()
    key = generation_key('macos' if macos else 'ios')
    if cached_interface(key, dm, workspace):
        return round(time.monotonic() - started, 2)
    romfs = restore(inputs, workspace / 'RomFS')
    environment = dict(os.environ, DM_ROOT=str(workspace), DM_ROMFS_SRC=str(romfs))
    environment.pop('DM_PC_DATA', None)
    environment.pop('DM_REQUIRE_PC_ART', None)
    if macos:
        environment['DM_OVERLAY_MAC'] = '1'
    if legacy:
        environment['DM_ASTC_PACK'] = str(legacy)
    def run(script, *args, **kwargs):
        before = time.monotonic()
        subprocess.run([sys.executable, str(dm / script), *map(str, args)],
                       env=environment, check=True, **kwargs)
        print(f'{script}: {time.monotonic() - before:.1f}s', flush=True)
    run('ios/tools/gen_assets.py', romfs)
    for output, script in (('dm_vault_tab.h', 'ios/tools/gen_vault_table.py'),
                           ('dm_hud_exp.h', 'ios/tools/gen_hud_exp.py'),
                           ('dm_bright_levels.inc', 'tools/gen_bright_levels.py')):
        # These tables depend on the same original game and their generator script.
        # Reuse them across unrelated interface changes; source changes regenerate them.
        if headers and baseline and git(dm.parent, 'show', baseline + ':dm/' + script) == (dm / script).read_text():
            shutil.copy2(headers / output, dm / 'host' / output)
            print('Reused game table:', output, flush=True)
            continue
        with (dm / 'host' / output).open('w') as stream:
            run(script, romfs, stdout=stream)
    overlay = workspace / 'romfs_overlay'
    for name in ('romfs_overlay', 'romfs_overlay_variants', 'romfs_overlay_astc', 'romfs_overlay_astc_variants'):
        shutil.rmtree(workspace / name, ignore_errors=True)
    run('ios/tools/gen_romfs_overlay.py', romfs, overlay)
    seconds = round(time.monotonic() - started, 2)
    save_interface(key, dm, workspace, seconds)
    return seconds


def ios_payload(directory, info, dm, destination, workspace):
    seconds = generate(dm, directory / 'romfs-inputs.zip', workspace, legacy=directory / 'legacy',
                       headers=directory / 'generated/host', baseline=info['producer']['source_commit'])
    stage = workspace / 'payload'
    shutil.rmtree(stage, ignore_errors=True)
    (stage / 'dm').mkdir(parents=True)
    with zipfile.ZipFile(directory / 'source-payload.zip') as source:
        for item in source.infolist():
            if not item.is_dir() and heavy_member(item.filename):
                path = stage / item.filename.removeprefix(BASE)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read(item))
                path.chmod(0o755 if path.name == 'Guest' else 0o644)
    for name in ('romfs_overlay', 'romfs_overlay_variants', 'romfs_overlay_astc', 'romfs_overlay_astc_variants'):
        shutil.copytree(workspace / name, stage / name)
    for name, source in (('hud_v4_ui', 'v4/hud_v4_ui'), ('mapgoal', 'mapgoal'), ('mercslots', 'mercslots')):
        shutil.copytree(dm / 'ios/Gen/assets/NativeHUD' / source, stage / name)
    for path in (dm / 'ios/Sources/TouchArt').iterdir():
        if path.is_file():
            shutil.copy2(path, stage / path.name)
    subprocess.run([sys.executable, str(dm / 'ios/tools/shell_stubs.py'), 'data', str(dm),
                    str(stage / 'dm/derived.tsv')], check=True)
    chain = info['payload']['shader_chain']
    product = (dm / 'PRODUCT').read_text().strip()
    (stage / 'dm/shell_info.txt').write_text(f'PRODUCT={product}\nCHAIN={chain}\nKIND=ios\n')
    (stage / 'dm/pack_info.txt').write_text(f'PRODUCT={product}\n')
    manifest = [p.name + '/' for p in stage.iterdir() if p.is_dir() and p.name != 'dm']
    manifest += [p.relative_to(stage).as_posix() for p in (stage / 'dm').iterdir() if p.is_file()]
    manifest += [p.name for p in stage.iterdir() if p.is_file()]
    (stage / 'dm/shell_manifest.txt').write_text('\n'.join(sorted(manifest)) + '\n')
    copy_licenses(dm, stage)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                output.write(path, BASE + path.relative_to(stage).as_posix())
    return seconds


def copy_licenses(dm, destination):
    licenses = [(dm.parent / 'LICENSE', 'd2r-mobile-MIT.txt')]
    licenses += [(p, p.relative_to(dm).as_posix()) for folder in ('third_party', 'gfx/nvn/include')
                 for p in (dm / folder).rglob('*') if p.is_file()
                 and (p.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')) or p.name == 'nvn.h')]
    for source, name in licenses:
        path = destination / 'Licenses' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)


if __name__ == '__main__':
    key = generation_key(sys.argv[1])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('interface_key=' + key + '\n')
