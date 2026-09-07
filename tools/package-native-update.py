#!/usr/bin/env python3
"""Build and verify a deterministic Bosun release archive. No device access."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "rpi-hub"))
from bosun_hub.update_package import validate_update_package


def package(uf2, output, release, firmware_version):
    binary = Path(uf2).read_bytes()
    manifest = {"schema": 1, "board": "midi-captain-rp2040", "release": release,
                "family": "native", "firmware_version": firmware_version,
                "flash_bytes": 8 * 1024 * 1024,
                "firmware_sha256": hashlib.sha256(binary).hexdigest()}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in (("manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()),
                               ("firmware.uf2", binary)):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        validate_update_package(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), **manifest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--uf2", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--firmware-version")
    args = parser.parse_args()
    if args.verify:
        validated = validate_update_package(args.verify)
        print(json.dumps({"path": str(args.verify), "sha256": hashlib.sha256(args.verify.read_bytes()).hexdigest(), **validated.manifest}, indent=2))
    else:
        if not all((args.uf2, args.output, args.release, args.firmware_version)):
            parser.error("provide --verify, or --uf2, --output, --release and --firmware-version")
        print(json.dumps(package(args.uf2, args.output, args.release, args.firmware_version), indent=2))
