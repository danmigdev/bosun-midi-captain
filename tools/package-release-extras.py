#!/usr/bin/env python3
"""Collect the matching offline guide, Pi setup, Stage and native update assets."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/release-extras')
    args = parser.parse_args()
    version = json.loads((ROOT / 'editor/package.json').read_text(encoding='utf-8'))['version']
    resources = ROOT / 'editor/src-tauri/resources'
    package = resources / 'update/bosun-update.zip'
    subprocess.run([sys.executable, str(ROOT / 'tools/verify-release-version.py'),
                    '--tag', f'v{version}', '--package', str(package)], check=True)
    sources = {
        f'Bosun-{version}-setup-guide.html': ROOT / 'dist/setup-guide/setup-guide.html',
        f'Bosun-{version}-pi-setup.tar.gz': resources / 'pi/bosun-pi-setup.tar.gz',
        f'Bosun-{version}-native-update.zip': package,
    }
    stage = ROOT / 'editor/dist-stage'
    for source in [*sources.values(), stage / 'index.html']:
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(f'Missing release input: {source}')
    args.output.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        shutil.copyfile(source, args.output / name)
    with zipfile.ZipFile(args.output / f'Bosun-{version}-stage.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_symlink():
                raise ValueError(f'Symlink in Stage: {path}')
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    print(f'Release extras ready: {args.output}')


if __name__ == '__main__':
    main()
