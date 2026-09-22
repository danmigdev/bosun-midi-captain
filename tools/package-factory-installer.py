#!/usr/bin/env python3
"""Package the RAM helper and empty storage with the exact bundled native release."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zipfile


def validate_loader(data):
    if len(data) < 1024 or len(data) % 512 or len(data) > 512 * 1024:
        raise ValueError("Invalid RAM UF2 length")
    for i in range(len(data) // 512):
        b = data[i * 512:(i + 1) * 512]
        words = struct.unpack_from("<8I", b)
        if words != (0x0A324655, 0x9E5D5157, 0x2000, 0x20000000 + i * 256,
                     256, i, len(data) // 512, 0xE48BFF56) or struct.unpack_from("<I", b, 508)[0] != 0x0AB16F30:
            raise ValueError("Helper must contain only contiguous RP2040 SRAM blocks")
        if words[3] + 256 > 0x20040000:
            raise ValueError("Helper exceeds SRAM")
    # SDK no_flash starts with executable entry code; vectors follow at 0x100.
    sp, pc = struct.unpack_from("<II", data, 512 + 32)
    if sp % 8 or not 0x20000000 < sp <= 0x20042000 or not pc & 1 or not 0x20000000 <= pc < 0x20000000 + len(data) // 2:
        raise ValueError("Invalid RAM vectors")


def verify(directory, package):
    directory = Path(directory)
    m = json.loads((directory / "manifest.json").read_text())
    with zipfile.ZipFile(package) as z:
        native = json.loads(z.read("manifest.json"))
    loader = (directory / "loader.uf2").read_bytes()
    storage = (directory / "storage.bin").read_bytes()
    validate_loader(loader)
    if (m["schema"] != 1 or m["firmware_sha256"] != native["firmware_sha256"] or
        m["loader_sha256"] != hashlib.sha256(loader).hexdigest() or
        m["storage_sha256"] != hashlib.sha256(storage).hexdigest() or
        len(storage) != 4194304 or b"littlefs" not in storage[3670016:3678208]):
        raise ValueError("Mismatched installer assets")
    return m


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--loader", type=Path)
    p.add_argument("--storage", type=Path)
    p.add_argument("--sdk", type=Path)
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    if not args.verify:
        if not all((args.loader, args.storage, args.sdk)):
            p.error("--loader, --storage and --sdk required to build")
        validate_loader(args.loader.read_bytes())
        args.output.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.package) as z:
            native = json.loads(z.read("manifest.json"))
        m = {"schema": 1, "firmware_sha256": native["firmware_sha256"]}
        for name, source in (("loader", args.loader), ("storage", args.storage)):
            m[name + "_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            shutil.copyfile(source, args.output / ("loader.uf2" if name == "loader" else "storage.bin"))
        (args.output / "manifest.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(args.sdk / "LICENSE.TXT", args.output / "pico-sdk-LICENSE.txt")
        shutil.copyfile(args.sdk / "lib/tinyusb/LICENSE", args.output / "tinyusb-LICENSE.txt")
        shutil.copyfile(args.sdk / "src/rp2_common/pico_stdio_usb/stdio_usb_descriptors.c", args.output / "usb-descriptors-MIT.c")
    print(json.dumps(verify(args.output, args.package), indent=2))
