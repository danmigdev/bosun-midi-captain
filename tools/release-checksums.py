#!/usr/bin/env python3
"""Require every release asset before creating the SHA256 manifest."""
import argparse
import hashlib
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'v\d+\.\d+\.\d+', args.tag):
        parser.error('Expected a stable vMAJOR.MINOR.PATCH tag')
    version = args.tag[1:]
    names = [
        f'Bosun-{version}-portable-x64.zip', f'Bosun_{version}_aarch64.dmg',
        f'Bosun_{version}_amd64.AppImage', 'bosun.apk',
        f'Bosun-{version}-setup-guide.html', f'Bosun-{version}-pi-setup.tar.gz',
        f'Bosun-{version}-native-update.zip', f'Bosun-{version}-stage.zip',
    ]
    lines = []
    for name in sorted(names):
        path = args.directory / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Missing or empty release asset: {name}')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        lines.append(f'{digest}  {name}\n')
    (args.directory / 'SHA256SUMS.txt').write_text(''.join(lines), encoding='utf-8', newline='\n')
    print(f'Checksummed all {len(names)} required release assets')


if __name__ == '__main__':
    main()
