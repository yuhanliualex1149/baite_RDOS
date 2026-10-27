"""Build a self-contained RDOS Skill from the same business rules as central."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'node_skills' / 'rdos-workflow'
SKILL_VERSION = '1.1.3'


def package_bytes(manifest: dict) -> tuple[bytes, dict]:
    """No writes or network: public node manifest in, installable ZIP + hashes out."""
    if (manifest.get('protocol_version') != 2 or not all(manifest.get(k) for k in
        ('workspace_id', 'project_id', 'node_id', 'base_token'))):
        raise ValueError('需要完整的公开节点登记文件')
    forbidden = {'env_id', 'generation', 'password', 'secret', 'token', 'api_key', 'credentials'}
    def contains_private(value):
        if isinstance(value, dict):
            return any(key in forbidden or contains_private(item) for key, item in value.items())
        if isinstance(value, list): return any(contains_private(item) for item in value)
        return False
    if contains_private(manifest):
        raise ValueError('Skill 包不能包含内部环境标识或凭据')
    files = {str(path.relative_to(SOURCE)): path.read_bytes() for path in SOURCE.rglob('*')
             if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    rules = (ROOT / 'server' / 'rdos_rules.py').read_bytes()
    rules_hash = hashlib.sha256(rules).hexdigest()
    files['scripts/rdos_rules.py'] = rules
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    files['version.json'] = json.dumps({'skill_version': SKILL_VERSION, 'protocol_version': 2,
        'business_rules_sha256': rules_hash}, sort_keys=True).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo('rdos-workflow/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    content = target.getvalue()
    return content, {'skill_version': SKILL_VERSION, 'protocol_version': 2,
        'business_rules_sha256': rules_hash, 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    content, metadata = package_bytes(json.loads(Path(args.manifest).read_text()))
    Path(args.output).write_bytes(content)
    print(json.dumps(metadata, sort_keys=True))


if __name__ == '__main__':
    main()
