"""Keep small original build inputs as encoded ranges, preserving upstream CASC addresses."""
import contextlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import zipfile

DATA_NAME = re.compile(r'\d{2}-\d{8}\.data')
MAX_INPUT_BYTES = 1024**3
MAX_LOGICAL_BYTES = 64 * 1024**3


@contextlib.contextmanager
def archive_reader(path):
    path = Path(path)
    if path.is_dir():
        yield {p.relative_to(path).as_posix(): p.stat().st_size for p in path.rglob('*') if p.is_file()}, lambda name: (path / name).open('rb')
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            yield {m.filename: m.file_size for m in archive.infolist() if not m.is_dir()}, archive.open
    else:
        with tarfile.open(path) as archive:
            yield {m.name: m.size for m in archive.getmembers() if m.isfile()}, archive.extractfile


def selected(path):
    path = path.lower()
    return (path.endswith(('.json', '.txt', '.ttf', '.otf', '.particles'))
            or '/global/excel/' in path
            or '/ui/' in path and path.endswith('.sprite'))


def ranges_for(manifest, config):
    ranges = {}
    def add(ekey, size):
        key = bytes.fromhex(ekey)
        if len(key) != 16 or size <= 0:
            raise ValueError('Invalid original resource address')
        name = f'{key[9]:02d}-{key[10]:08d}.data'
        start = int.from_bytes(key[11:], 'big')
        ranges.setdefault(name, []).append((start, start + size))
    for line in manifest.splitlines():
        fields = line.split('|')
        if len(fields) >= 11 and fields[7].isdigit() and selected(fields[9]):
            add(fields[6], int(fields[7]))
    values = {key.strip(): value.strip() for line in config.splitlines() if '=' in line
              for key, value in [line.split('=', 1)]}
    for name in ('vfs-1', 'vfs-root'):
        add(values[name].strip().split()[1], int(values[name + '-size'].split()[1]))
    for name, entries in ranges.items():
        merged = []
        for start, end in sorted(entries):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        ranges[name] = merged
    if sum(end - start for entries in ranges.values() for start, end in entries) > MAX_INPUT_BYTES:
        raise ValueError('Selected original build inputs exceed 1 GiB')
    return ranges


def create(original_archive, destination):
    """Read only selected ranges; never expand the multi-GB original archive to disk."""
    with archive_reader(original_archive) as (names, read):
        manifests = [n for n in names if n.endswith('RomFS/data/build_manifest.psv')]
        if len(manifests) != 1:
            raise ValueError('Original archive must contain one RomFS/data/build_manifest.psv')
        prefix = manifests[0].removesuffix('build_manifest.psv')
        with read(manifests[0]) as stream:
            manifest = stream.read()
        with read(prefix + '.build.config') as stream:
            config = stream.read()
        ranges = ranges_for(manifest.decode(), config.decode())
        sizes = {n.removeprefix(prefix): size for n, size in names.items()
                 if n.startswith(prefix) and DATA_NAME.fullmatch(n.removeprefix(prefix))}
        if sum(sizes.values()) > MAX_LOGICAL_BYTES or not ranges.keys() <= sizes.keys():
            raise ValueError('Original resource archive addresses are incomplete')
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            output.writestr('data/build_manifest.psv', manifest)
            output.writestr('data/.build.config', config)
            output.writestr('ranges.json', json.dumps({'sizes': sizes, 'ranges': ranges}))
            for name, entries in sorted(ranges.items()):
                with read(prefix + name) as source, output.open('ranges/' + name, 'w') as target:
                    position = 0
                    for start, end in entries:
                        if end > sizes[name]:
                            raise ValueError('Original resource range exceeds its file')
                        # Forward reads also work with streaming compressed archive members.
                        while position < start:
                            data = source.read(min(start - position, 8 * 1024**2))
                            if not data:
                                raise ValueError('Original resource archive is truncated')
                            position += len(data)
                        left = end - start
                        while left:
                            data = source.read(min(left, 8 * 1024**2))
                            if not data:
                                raise ValueError('Original resource archive is truncated')
                            target.write(data)
                            left -= len(data)
                            position += len(data)
    return sum(end - start for entries in ranges.values() for start, end in entries)


def restore(archive, romfs):
    """Restore sparse original-address files for unmodified upstream generators."""
    romfs = Path(romfs)
    shutil.rmtree(romfs, ignore_errors=True)
    data = romfs / 'data'
    data.mkdir(parents=True)
    with zipfile.ZipFile(archive) as source:
        info = json.loads(source.read('ranges.json'))
        sizes, ranges = info['sizes'], info['ranges']
        if (not ranges.keys() <= sizes.keys() or len(sizes) > 256
                or any(not DATA_NAME.fullmatch(n) or type(s) is not int or s <= 0 for n, s in sizes.items())
                or sum(sizes.values()) > MAX_LOGICAL_BYTES):
            raise ValueError('Invalid original resource range index')
        expected = {'ranges.json', 'data/build_manifest.psv', 'data/.build.config',
                    *('ranges/' + n for n in ranges)}
        if set(source.namelist()) != expected or len(source.namelist()) != len(expected):
            raise ValueError('Unexpected files in original resource input')
        total = 0
        for name, entries in ranges.items():
            previous = 0
            for start, end in entries:
                if type(start) is not int or type(end) is not int or not previous <= start < end <= sizes[name]:
                    raise ValueError('Invalid or overlapping original resource ranges')
                previous = end
                total += end - start
            if source.getinfo('ranges/' + name).file_size != sum(end - start for start, end in entries):
                raise ValueError('Original resource range length mismatch')
        if total > MAX_INPUT_BYTES:
            raise ValueError('Original resource input is too large')
        for name in ('build_manifest.psv', '.build.config'):
            if source.getinfo('data/' + name).file_size > 64 * 1024**2:
                raise ValueError('Original resource metadata is too large')
            (data / name).write_bytes(source.read('data/' + name))
        for name, size in sizes.items():
            with (data / name).open('wb') as target:
                target.truncate(size)  # Holes occupy no physical disk space.
                if name not in ranges:
                    continue
                with source.open('ranges/' + name) as stream:
                    for start, end in ranges[name]:
                        target.seek(start)
                        left = end - start
                        while left:
                            block = stream.read(min(left, 8 * 1024**2))
                            if not block:
                                raise ValueError('Original resource input is truncated')
                            target.write(block)
                            left -= len(block)
    return romfs
