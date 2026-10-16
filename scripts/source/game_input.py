"""Download, unpack and validate the original ExeFS/RomFS input for both platforms."""
import json
from pathlib import PurePosixPath
import re
import shutil

import ci
from archive_input import extract
from input_config import download_file, input_identity, settings

MAX_GAME_BYTES = 24 * 1024**3


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def game_path(upstream, workspace):
    match = re.search(r'^GAME=\$DM_ROOT/(data/switch/[^\s;]+)(?:[;\s]|$)',
                      (upstream / 'setup.sh').read_text(), re.M)
    if not match or '..' in PurePosixPath(match[1]).parts or '$' in match[1]:
        raise ValueError('Cannot determine the game directory from upstream setup.sh')
    return workspace / match[1]


def download():
    url, digest = settings('GAME_ARCHIVE')
    archive = ci.ROOT / '.game-downloads/game-input.archive'
    marker = archive.with_suffix('.json')
    if archive.is_file():
        actual = ci.sha256(archive)
        if digest:
            valid = actual == digest
        else:
            try:
                saved = json.loads(marker.read_text())
                valid = (saved['identity'] == input_identity(url, digest)
                         and saved['bytes'] == archive.stat().st_size
                         and saved.get('sha256', actual) == actual)
            except (OSError, ValueError, KeyError):
                valid = False
        if valid:
            print('Reused verified original game archive; download skipped', flush=True)
            return
    download_file(url, archive, digest or None, max_bytes=MAX_GAME_BYTES)
    if not archive.stat().st_size:
        raise ValueError('Game archive is empty')
    write(marker, {'identity': input_identity(url, digest), 'bytes': archive.stat().st_size,
                   'sha256': ci.sha256(archive)})
    print('Original game archive downloaded and verified; ready to cache', flush=True)


def validate_game(game):
    for name, digest in ci.CONFIG['nso_sha256'].items():
        if ci.sha256(game / 'ExeFS' / name) != digest:
            raise ValueError('Game program differs from the selected source: ' + name)
    if not (game / 'RomFS/data/build_manifest.psv').is_file():
        raise ValueError('RomFS/data/build_manifest.psv is missing')
    for name, size in ci.resource_sizes().items():
        if (game / 'RomFS/data' / name).stat().st_size != size:
            raise ValueError('Game resource size mismatch: ' + name)


def unpack(workspace):
    upstream = ci.ROOT / 'upstream'
    archive = ci.ROOT / '.game-downloads/game-input.archive'
    game = game_path(upstream, workspace)
    temporary = workspace / 'unpack'
    shutil.rmtree(temporary, ignore_errors=True)
    extract(archive, temporary, max_bytes=MAX_GAME_BYTES, max_members=100000)
    candidates = [p.parent for p in temporary.rglob('ExeFS')
                  if p.is_dir() and (p.parent / 'RomFS').is_dir()]
    if len(candidates) != 1:
        raise ValueError('Archive must contain exactly one ExeFS/RomFS pair')
    validate_game(candidates[0])
    game.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(game, ignore_errors=True)
    shutil.move(str(candidates[0]), game)
    shutil.rmtree(temporary, ignore_errors=True)
    return game
