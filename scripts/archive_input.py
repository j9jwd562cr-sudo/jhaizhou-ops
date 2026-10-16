"""Safely extract build inputs without links, duplicate paths or special files."""
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import zipfile

MAX_EXPANDED_BYTES = 2 * 1024**3


def safe_members(members, max_bytes=MAX_EXPANDED_BYTES, max_members=32):
    """Check every path and size before writing any member."""
    seen, total = set(), 0
    members = list(members)
    if len(members) > max_members:
        raise ValueError('Archive has too many files')
    for name, size, is_directory, is_regular in members:
        if is_directory and name in ('.', './'):
            continue
        path = PurePosixPath(name)
        if ('\\' in name or '\x00' in name or path.is_absolute() or
                '..' in path.parts or not path.parts or ':' in path.parts[0]):
            raise ValueError('Unsafe archive path')
        normalized = str(path).casefold()
        if normalized in seen or (not is_directory and not is_regular):
            raise ValueError('Duplicate path, link or special file in archive')
        seen.add(normalized)
        if path.name == 'prepared.json' and size > 1024**2:
            raise ValueError('Prepared manifest is too large')
        total += size
        if size < 0 or total > max_bytes:
            raise ValueError('Archive exceeds the supported extracted size')
    return total


def extract(archive, target, max_bytes=MAX_EXPANDED_BYTES, max_members=32):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            total = safe_members(((m.filename, m.file_size, m.is_dir(),
                                  stat.S_IFMT(m.external_attr >> 16) in (0, stat.S_IFREG))
                                 for m in members), max_bytes=max_bytes, max_members=max_members)
            if shutil.disk_usage(target).free < total + 1024**3:
                raise ValueError('Not enough free disk space to extract the archive')
            for member in members:
                dest = target / member.filename
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as src, dest.open('xb') as dst:
                        shutil.copyfileobj(src, dst, 8 * 1024**2)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as source:
            members = source.getmembers()
            total = safe_members(((m.name, m.size, m.isdir(), m.isfile()) for m in members),
                                 max_bytes=max_bytes, max_members=max_members)
            if shutil.disk_usage(target).free < total + 1024**3:
                raise ValueError('Not enough free disk space to extract the archive')
            for member in members:
                if member.isdir() and member.name in ('.', './'):
                    continue
                dest = target / member.name
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as src, dest.open('xb') as dst:
                        shutil.copyfileobj(src, dst, 8 * 1024**2)
    else:
        raise ValueError('Expected a ZIP, tar.gz or tar.xz archive')
