#!/usr/bin/env python3
"""Sign the Raspberry Pi setup package for Desktop self-updates, or check a signature.

The Pi installs a Desktop upload as root only when this OpenSSH signature
verifies against /etc/bosun/allowed_signers (tools/rpi-hub/allowed_signers).
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / 'editor/src-tauri/resources/pi/bosun-pi-setup.tar.gz'
ALLOWED_SIGNERS = ROOT / 'tools/rpi-hub/allowed_signers'
NAMESPACE, IDENTITY = 'bosun-pi-package', 'bosun-release'
MAX_PACKAGE, MAX_SIGNATURE = 4 * 1024 * 1024, 4096
BEGIN, END = '-----BEGIN SSH SIGNATURE-----', '-----END SSH SIGNATURE-----'


class SigningError(Exception):
    pass


def keygen():
    found = os.environ.get('BOSUN_SSH_KEYGEN') or shutil.which('ssh-keygen')
    if not found:
        raise SigningError('ssh-keygen (OpenSSH 8.2 or later) is required')
    return found


def has_signers(path):
    """A trust file without any principal line cannot verify anything yet."""
    if not path.is_file():
        return False
    lines = path.read_text(encoding='utf-8').splitlines()
    return any(line.strip() and not line.lstrip().startswith('#') for line in lines)


def read_package(package):
    data = package.read_bytes()
    if not data or len(data) > MAX_PACKAGE or data[:2] != b'\x1f\x8b':
        raise SigningError(f'{package} is not a gzip package of at most {MAX_PACKAGE} bytes')
    return data


def run(args, data):
    # stdin mode never prompts: signing a file path asks before overwriting
    # an existing .sig and then exits 0 without writing it.
    result = subprocess.run(args, input=data, capture_output=True, timeout=60)
    return result.returncode, result.stdout, result.stderr.decode('utf-8', 'replace').strip()


def check(package, signature, allowed, *, require_signers=False):
    data = read_package(package)
    if not signature.is_file():
        raise SigningError(f'Missing signature: {signature}')
    text = signature.read_bytes()
    if len(text) > MAX_SIGNATURE or not text.decode('ascii', 'replace').strip().startswith(BEGIN):
        raise SigningError(f'{signature} is not an SSH signature of at most {MAX_SIGNATURE} bytes')
    if has_signers(allowed):
        args = [keygen(), '-Y', 'verify', '-f', str(allowed), '-I', IDENTITY, '-n', NAMESPACE, '-s', str(signature)]
    elif require_signers:
        raise SigningError(f'No release key in {allowed}')
    else:
        print(f'warning: no release key in {allowed}; checking the signature without a trusted signer', file=sys.stderr)
        args = [keygen(), '-Y', 'check-novalidate', '-n', NAMESPACE, '-s', str(signature)]
    code, _, error = run(args, data)
    if code:
        raise SigningError(f'{signature} does not match {package.name}: {error or "verification failed"}')


def sign(package, key, allowed, *, require_signers=False):
    if not key:
        raise SigningError('Set BOSUN_PI_SIGNING_KEY to the path of the Pi release signing key')
    if not Path(key).is_file():
        raise SigningError(f'Pi signing key not found: {key}')
    signature = package.with_name(package.name + '.sig')
    code, out, error = run([keygen(), '-Y', 'sign', '-f', str(key), '-n', NAMESPACE], read_package(package))
    # Windows OpenSSH writes CRLF to stdout; the armor itself is line-neutral.
    text = out.replace(b'\r\n', b'\n')
    if code or not text.startswith(BEGIN.encode()) or END.encode() not in text or len(text) > MAX_SIGNATURE:
        raise SigningError(f'ssh-keygen could not sign {package.name}: {error or "no signature"}')
    temporary = signature.with_name(signature.name + '.tmp')
    temporary.write_bytes(text)
    temporary.replace(signature)
    try:
        check(package, signature, allowed, require_signers=require_signers)
    except SigningError:
        signature.unlink(missing_ok=True)
        raise
    return signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=PACKAGE)
    parser.add_argument('--key', default=os.environ.get('BOSUN_PI_SIGNING_KEY', ''),
                        help='private key path (default: BOSUN_PI_SIGNING_KEY)')
    parser.add_argument('--allowed-signers', type=Path, default=ALLOWED_SIGNERS)
    parser.add_argument('--check', action='store_true', help='only verify the existing <package>.sig')
    parser.add_argument('--require-allowed-signers', action='store_true',
                        help='fail unless the signature verifies against a release key')
    args = parser.parse_args()
    try:
        if args.check:
            check(args.package, args.package.with_name(args.package.name + '.sig'), args.allowed_signers,
                  require_signers=args.require_allowed_signers)
            print(f'Signature matches: {args.package}')
        else:
            signature = sign(args.package, args.key, args.allowed_signers,
                             require_signers=args.require_allowed_signers)
            print(f'Signed and verified: {signature}')
    except (OSError, SigningError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Pi package signing failed: {error}\n')


if __name__ == '__main__':
    main()
