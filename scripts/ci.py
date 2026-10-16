#!/usr/bin/env python3
"""Shared input validation and native executable inspection."""
import hashlib
import json
from pathlib import Path
import re
import struct
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {}
CONTEXT = ROOT / 'outputs/build-context.json'
if CONTEXT.is_file():
    CONFIG.update(json.loads(CONTEXT.read_text()))


def build_label(config=None):
    config = CONFIG if config is None else config
    label = config.get('build_label', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', label):
        raise ValueError('Resolve the selected source before naming build outputs')
    return label


def ipa_names(config=None):
    config = CONFIG if config is None else config
    label = release_tag(config)
    return (f'D2RMobile-{label}.ipa', f'D2RMobile-{label}-ios18.ipa')


def release_tag(config=None):
    config = CONFIG if config is None else config
    label = config.get('release_tag') or build_label(config)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', label):
        raise ValueError('Resolve the selected source before naming build outputs')
    return label


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def download(url, dest, expected=None, max_bytes=None):
    """Keep signed URLs out of logs; reject web landing pages."""
    if not url.startswith('https://'):
        raise ValueError('Input downloads must use HTTPS')
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + '.part')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'd2r-mobile-build/1.0'})
        with urllib.request.urlopen(req, timeout=120) as r:
            if 'text/html' in r.headers.get('Content-Type', '').lower():
                raise ValueError('Download returned an HTML page; supply a direct file URL')
            length = r.headers.get('Content-Length')
            if max_bytes and length is not None and int(length) > max_bytes:
                raise ValueError('Download exceeds the supported size')
            with partial.open('wb') as f:
                while block := r.read(8 * 1024 * 1024):
                    f.write(block)
                    if max_bytes and f.tell() > max_bytes:
                        raise ValueError('Download exceeds the supported size')
            if length is not None and partial.stat().st_size != int(length):
                raise ValueError('Download incomplete: ' + dest.name)
        if expected and sha256(partial) != expected.lower():
            raise ValueError('Download SHA256 mismatch: ' + dest.name)
        partial.replace(dest)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        # urllib exceptions can contain signed URL credentials.
        if isinstance(exc, ValueError):
            raise
        raise RuntimeError(f'Download failed for {dest.name}: {type(exc).__name__}') from None


def resource_sizes():
    src = (ROOT / 'upstream/dm/host/romfs_validate.c').read_text()
    names = re.search(r'static const char \*need\[\] = \{(.*?)\};', src, re.S)
    sizes = re.search(r'static const long long size\[\] = \{(.*?)\};', src, re.S)
    if names is None or sizes is None:
        raise ValueError('Cannot read upstream resource table')
    files = re.findall(r'"([^"]+\.data)"', names.group(1))
    lengths = [int(n) for n in re.findall(r'\d+', sizes.group(1))]
    if not files or len(files) != len(lengths) or len(set(files)) != len(files):
        raise ValueError('Unexpected upstream resource manifest')
    return dict(zip(files, lengths))


def macho_minimum_os(data, platform=2):
    """Read the minimum system from a thin ARM64 executable's load commands."""
    if len(data) < 32 or struct.unpack_from('<II', data) != (0xfeedfacf, 0x0100000c):
        raise ValueError('Native executable is not Mach-O arm64')
    count, size = struct.unpack_from('<II', data, 16)
    end = 32 + size
    if end > len(data) or count > size // 8:
        raise ValueError('Truncated Mach-O load commands')
    offset, versions = 32, []
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError('Truncated Mach-O load command')
        command, length = struct.unpack_from('<II', data, offset)
        if length < 8 or length % 8 or offset + length > end:
            raise ValueError('Invalid Mach-O load command size')
        if command == 0x32:  # LC_BUILD_VERSION
            if length < 24:
                raise ValueError('Truncated Mach-O build version')
            target, minimum = struct.unpack_from('<II', data, offset + 8)
            if target != platform:
                raise ValueError('Native executable targets the wrong platform')
            versions.append((minimum >> 16, (minimum >> 8) & 255, minimum & 255))
        offset += length
    if offset != end or len(versions) != 1:
        raise ValueError('Missing or ambiguous Mach-O build version')
    return versions[0]
