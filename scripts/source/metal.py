"""Validate precompiled Metal libraries and their reflection and manifest metadata."""
import json
import zipfile

BASE = 'Payload/DmIOS.app/'


def verify_payload(payload, report):
    if not report.get('metal_precompiled'):
        raise ValueError('Precompiled Metal shader report missing')
    chain = report['shader_chain']
    prefix = BASE + 'msl/b-' + chain + '/'
    with zipfile.ZipFile(payload) as z:
        names = z.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate compiled payload member')
        libraries = [n for n in names if n.endswith('.metallib')]
        if len(libraries) != report['metal_libraries'] or not libraries:
            raise ValueError('Incomplete precompiled Metal libraries')
        for name in libraries:
            if not name.startswith(prefix) or not z.read(name).startswith(b'MTLB'):
                raise ValueError('Uncompiled or mixed Metal shader: ' + name)
            if name.endswith('_b.metallib'):
                reflection = json.loads(z.read(name.removesuffix('.metallib') + '.json'))
                if reflection['chain'] != chain or reflection['chain_metal'] != report['metal_toolchain']['compile_record']:
                    raise ValueError('Compiled reflection metadata mismatch')
        info = dict(line.split('=', 1) for line in z.read(BASE + 'dm/shell_info.txt').decode().splitlines())
        if info['CHAIN'] != chain or info['PRODUCT'] != report['product']:
            raise ValueError('Compiled shell metadata mismatch')
        if not z.read(BASE + 'dm/c1_full.tsv').startswith(('# chain b-' + chain + ' ').encode()):
            raise ValueError('Compiled shader index mismatch')
        for item in z.read(BASE + 'dm/shell_manifest.txt').decode().splitlines():
            target = BASE + item
            if not item or not (any(n.startswith(target) for n in names) if item.endswith('/') else target in names):
                raise ValueError('Incomplete compiled payload manifest: ' + item)
