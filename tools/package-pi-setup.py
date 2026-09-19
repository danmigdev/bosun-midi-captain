#!/usr/bin/env python3
"""Package built Stage and installation sources; never access a Pi or SD card."""
import argparse
import gzip
import io
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def package(output):
    if not (ROOT / 'editor/dist-stage/index.html').is_file():
        raise ValueError('Build Stage first: npm --prefix editor run build:stage')
    files = set()
    for directory in ('editor/dist-stage', 'tools/rpi-hub/bosun_hub', 'tools/rpi-hub/kiosk',
                      'tools/rpi-hub/systemd', 'tools/rpi-hub/udev', 'tools/rpi-hub/boot-splash',
                      'firmware-native/include', 'firmware-native/third_party/littlefs'):
        for p in (ROOT / directory).rglob('*'):
            if p.is_symlink():
                raise ValueError(f'Symlink in setup input: {p}')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
                files.add(p.relative_to(ROOT).as_posix())
    files.update(('tools/rpi-hub/README.md', 'tools/rpi-hub/requirements.txt',
                  'tools/rpi-hub/install.sh', 'tools/rpi-hub/install-native-updater.sh',
                  'tools/rpi-hub/quick-install.sh', 'tools/rpi-hub/install-boot-splash.sh',
                  'tools/rpi-hub/prepare-framebuffer-splash.sh',
                  'firmware-native/platform/host/storage_image.c', 'firmware-native/platform/rp2040/storage.c',
                  'firmware-native/src/storage_path.c', 'firmware-native/src/config.c', 'firmware-native/src/json.c',
                  'editor/package.json', 'editor/src-tauri/icons/icon.png', 'LICENSE'))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            for name in sorted(files):
                data = (ROOT / name).read_bytes()
                if name.endswith(('.sh', '.py', '.service', '.timer', '.rules')):
                    data = data.replace(b'\r\n', b'\n')
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                tar.addfile(info, io.BytesIO(data))
    return len(files)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / 'editor/src-tauri/resources/pi/bosun-pi-setup.tar.gz')
    args = p.parse_args()
    print(f'Packaged {package(args.output)} files: {args.output}')
