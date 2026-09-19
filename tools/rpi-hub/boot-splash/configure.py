#!/usr/bin/env python3
"""Install the optional Pi boot branding; --root supports isolated fixture tests."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[2]
BEGIN = "# BEGIN BOSUN BOOT SPLASH"
END = "# END BOSUN BOOT SPLASH"
BOOT_OPTIONS = (
    "quiet", "nosplash", "plymouth.enable=0", "bosun.quiet=1", "loglevel=0", "logo.nologo",
    "vt.global_cursor_default=0", "systemd.show_status=false", "rd.systemd.show_status=false",
)
# Retire only files created by the earlier, unsuccessful Plymouth experiment.
OBSOLETE_UNITS = {
    "bosun-boot-splash.service": "Description=Bosun boot screen after the root filesystem is available",
    "bosun-kiosk.service.d/50-boot-splash.conf": "ExecStartPre=-+/usr/bin/timeout 5 /usr/bin/plymouth quit --retain-splash",
    "plymouth-quit.service.d/50-bosun.conf": "ExecStart=-/usr/bin/plymouth quit --retain-splash",
    "plymouth-quit-wait.service.d/50-bosun.conf": "[Unit]\nAfter=bosun-kiosk.service\n",
}


def quiet_cmdline(original: str) -> str:
    lines = [line for line in original.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("cmdline.txt must contain exactly one non-empty line")
    tokens = lines[0].split()
    if len([token for token in tokens if token.startswith("root=")]) != 1:
        raise ValueError("cmdline.txt must contain exactly one root= parameter")
    keys = {option.split("=", 1)[0] for option in BOOT_OPTIONS} | {
        "splash", "plymouth.ignore-serial-consoles", "bosun.splash",
    }
    # Keep a virtual console for diagnostics, but route /dev/console writes
    # (including initramfs fsck and cloud-init) away from the visible tty1.
    tokens = [token for token in tokens if token.split("=", 1)[0] not in keys]
    tokens = [re.sub(r"^console=tty[01](?=,|$)", "console=tty3", token) for token in tokens]
    return " ".join([*tokens, *BOOT_OPTIONS]) + "\n"


def quiet_config(original: str) -> str:
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise ValueError("Malformed Bosun splash block in config.txt")
    original = re.sub(r"(?m)^" + re.escape(BEGIN) + r"\n.*?^" + re.escape(END) + r"\n?",
                      "", original, flags=re.DOTALL)
    return original.rstrip() + f"\n\n{BEGIN}\n[all]\ndisable_splash=1\n{END}\n"


def atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    previous = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=".bosun-splash-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, previous.st_mode & 0o777 if previous else 0o644)
        if previous and hasattr(os, "chown"):
            os.chown(temporary, previous.st_uid, previous.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def brand_stage(existing: str, template: str, version: str, logo: bytes) -> str:
    """Add only boot markup to the installed page; preserve its deployed JS/CSS."""
    for section, closing in (("STYLE", "</head>"), ("SPLASH", "</body>")):
        pattern = rf"<!-- BOSUN BOOT {section} BEGIN -->.*?<!-- BOSUN BOOT {section} END -->"
        block = re.search(pattern, template, flags=re.DOTALL)
        if block is None or existing.count(closing) != 1:
            raise ValueError("Stage HTML is missing its boot template or document structure")
        markup = block.group().replace("__BOSUN_VERSION__", version).replace(
            "__BOSUN_BOOT_LOGO__", "data:image/png;base64," + base64.b64encode(logo).decode("ascii"))
        if re.search(pattern, existing, flags=re.DOTALL):
            existing = re.sub(pattern, lambda _: markup, existing, flags=re.DOTALL)
        else:
            existing = existing.replace(closing, markup + "\n" + closing)
    return existing


def configure(root: Path, *, check: bool = False, repo: Path = REPO) -> str:
    root = root.resolve()
    version = json.loads((repo / "editor/package.json").read_text(encoding="utf-8"))["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid Bosun release version")
    logo = (repo / "editor/src-tauri/icons/icon.png").read_bytes()
    if not logo.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The Bosun icon must be a PNG")
    boot = root / "boot/firmware"
    originals = {name: (boot / name).read_bytes() for name in ("config.txt", "cmdline.txt")}
    config = quiet_config(originals["config.txt"].decode("utf-8").replace("\r\n", "\n"))
    cmdline = quiet_cmdline(originals["cmdline.txt"].decode("utf-8"))
    stage = root / "opt/bosun-hub/stage/index.html"
    page = brand_stage(stage.read_text(encoding="utf-8"),
                       (repo / "editor/stage-kiosk.html").read_text(encoding="utf-8"), version, logo)
    files = {
        "boot/firmware/config.txt": config.encode(),
        "boot/firmware/cmdline.txt": cmdline.encode(),
        "opt/bosun-hub/stage/index.html": page.encode(),
        "etc/systemd/system/getty@tty1.service.d/50-bosun-splash.conf": b"""[Unit]
# Removing 'bosun.quiet=1' also restores the local login console.
ConditionKernelCommandLine=!bosun.quiet=1
""",
    }
    # Validate all destinations before changing any file, including fixture roots.
    for name in files:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe splash destination: {path}")
    obsolete = []
    for name, signature in OBSOLETE_UNITS.items():
        path = root / "etc/systemd/system" / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe legacy splash unit: {path}")
        if path.exists():
            if signature not in path.read_text(encoding="utf-8"):
                raise ValueError(f"Legacy splash unit was modified; inspect it before removal: {path}")
            obsolete.append(path)
    if not check:
        for name, content in originals.items():
            backup = boot / (name + ".bosun-before-splash")
            if not backup.exists():
                atomic_write(backup, content)
        for name, content in files.items():
            atomic_write(root / name, content)
        for path in obsolete:
            path.unlink()
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        print(configure(args.root, check=args.check))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Bosun boot splash: {error}\n")
