#!/usr/bin/env python3
"""Check release identities and an optional firmware update archive."""
import argparse
import json
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parent.parent


def verify(root=ROOT, *, tag=None, package=None):
    root = Path(root)
    metadata = json.loads((root / "editor/package.json").read_text(encoding="utf-8"))
    release = metadata["version"]
    channel = metadata.get("bosunReleaseChannel", "stable")
    if channel not in ("stable", "experimental"):
        raise ValueError("Unknown release channel")
    if not isinstance(release, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release):
        raise ValueError("Editor release must be a plain X.Y.Z version")
    firmware_version = release
    expected = {
        "editor/src-tauri/tauri.conf.json": (json.loads((root / "editor/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"], release),
        "editor/src-tauri/Cargo.toml": (tomllib.loads((root / "editor/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]["version"], release),
    }
    patterns = {
        "firmware/include/bosun/protocol.h": (r'^#define BOSUN_FIRMWARE_VERSION\s+"([^"]+)"\s*$', firmware_version, 1),
        "firmware/CMakeLists.txt": (r'project\(BosunNative\s+VERSION\s+(\S+)\s+LANGUAGES\b', release, 2),
        "firmware/platform/rp2040/CMakeLists.txt": (r'pico_set_program_version\(\$\{target\}\s+"([^"]+)"\)', firmware_version, 1),
        # HUB_INFO reports it; Desktop requires the release after a Pi update.
        "tools/rpi-hub/bosun_hub/__init__.py": (r'^__version__\s*=\s*"([^"]+)"\s*$', release, 1),
    }
    for path, (pattern, wanted, count) in patterns.items():
        versions = re.findall(pattern, (root / path).read_text(encoding="utf-8"), re.MULTILINE)
        if len(versions) != count or any(version != wanted for version in versions):
            raise ValueError(f"{path}: expected {count} declaration(s) of {wanted}, found {versions}")
    for path, (actual, wanted) in expected.items():
        if actual != wanted:
            raise ValueError(f"{path}: expected {wanted}, found {actual}")
    if tag is not None and tag.removeprefix("refs/tags/") != "v" + release:
        raise ValueError(f"Release tag must be v{release}, found {tag}")
    if package is not None:
        sys.path.insert(0, str(ROOT / "tools/rpi-hub"))
        from bosun_hub.update_package import validate_update_package
        validated = validate_update_package(Path(package))
        for key, wanted in (("release", release), ("firmware_version", firmware_version)):
            if validated.manifest[key] != wanted:
                raise ValueError(f"Firmware update manifest {key}: expected {wanted}, found {validated.manifest[key]}")
        binary = b"".join(validated.firmware_pages[address] for address in sorted(validated.firmware_pages))
        # Every NUL-delimited version string in the image, so a stale build
        # cannot hide behind a fresh manifest. Besides the firmware's own
        # version, binary info carries the pinned Pico SDK version.
        sdk = re.search(r"Pico SDK ([0-9]+\.[0-9]+\.[0-9]+)",
                        (root / "firmware/cmake/pico_sdk.cmake").read_text(encoding="utf-8"))
        own = firmware_version.encode("ascii")
        allowed = {own} | ({sdk[1].encode("ascii")} if sdk else set())
        versions = set(re.findall(rb"(?<=\x00)[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?=\x00)", binary))
        if own not in versions or not versions <= allowed:
            raise ValueError(f"Firmware UF2 must embed only firmware version {firmware_version}, found {sorted(versions)}")
    return {"release": release, "firmware_version": firmware_version, "prerelease": str(channel == "experimental").lower()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require the selected release tag to match the checkout")
    parser.add_argument("--package", type=Path, help="Require a valid firmware package from this release")
    parser.add_argument("--github-output", type=Path, help="Append checked release versions to GITHUB_OUTPUT")
    args = parser.parse_args()
    try:
        result = verify(tag=args.tag, package=args.package)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as out:
                for key, value in result.items():
                    out.write(f"{key}={value}\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Release version check failed: {error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
