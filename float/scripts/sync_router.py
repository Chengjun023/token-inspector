#!/usr/bin/env python3
"""Vendor the router's shared, dependency-free definitions for standalone Float builds."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pairs = [('scripts/difficulty.py', 'backend/difficulty.py'),
             ('scripts/model_registry.py', 'backend/model_registry.py'),
             ('data/models.bundled.json', 'data/models.bundled.json')]
    hashes = {}
    for source, target in pairs:
        data = (args.source / source).read_bytes()
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[target] = hashlib.sha256(data).hexdigest()
    (root / 'data/router-vendor.json').write_text(json.dumps(
        {'schema_version':1, 'source':'adaptive-router', 'files':hashes}, indent=2)+'\n')
    print('Synced shared scoring, registry and bundled prices.')


if __name__ == '__main__':
    main()
