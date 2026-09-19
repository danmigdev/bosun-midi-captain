#!/usr/bin/env python3
"""Stage a guarded early service trial without replacing any normal boot file."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from configure import atomic_write, quiet_cmdline

PROTECTED = ("config.txt", "cmdline.txt", "initramfs8", "kernel8.img", "start.elf", "bootcode.bin")
ASSET_FILES = ("bosun-boot-picture", "splash.bin", "splash-45.bin", "splash-65.bin",
               "splash-80.bin", "show.sh")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def services(assets: str, token: str | None = None):
    """Order short draws at milestones, completing all writes before Cage starts."""
    base = "bosun-framebuffer-splash" + ("-trial" if token else "")
    condition = (f"ConditionKernelCommandLine=bosun.splash_trial={token}\n" if token else
                 "ConditionKernelCommandLine=bosun.framebuffer_splash=1\n"
                 "ConditionKernelCommandLine=!bosun.splash_trial\n")
    milestones = (
        (20, "", "local-fs.target systemd-udev-trigger.service console-setup.service", "sysinit.target", "sysinit.target"),
        (45, "-45", f"sysinit.target {base}.service", "basic.target", "basic.target"),
        (65, "-65", f"network.target {base}-45.service", "bosun-hub.service", "multi-user.target"),
        (80, "-80", f"bosun-hub.service {base}-65.service", "", "multi-user.target"),
    )
    return {f"{base}{suffix}.service": (
        f"[Unit]\nDescription=Bosun boot progress {percent}%%\nDefaultDependencies=no\n"
        f"After={after}\nBefore=bosun-kiosk.service {before}\n" + condition +
        "\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=-{assets}/show.sh {percent}\nTimeoutStartSec=3\nTimeoutStopSec=1\n"
        "StandardOutput=journal\nStandardError=journal\n\n[Install]\n"
        f"WantedBy={target}\n")
        for percent, suffix, after, before, target in milestones}


def stage(root: Path, build: Path):
    root, build = root.resolve(), build.resolve()
    boot = root / "boot/firmware"
    config = (boot / "config.txt").read_text()
    if "auto_initramfs=1" not in config or re.search(r"(?m)^\s*initramfs\s", config):
        raise ValueError("Requires the standard automatic initramfs8 configuration")
    if (boot / "autoboot.txt").exists():
        raise ValueError("Inspect the existing autoboot configuration before using this trial")
    trial = boot / "tryboot.txt"
    if trial.exists() and not any(marker in trial.read_text() for marker in (
            "# BOSUN FRAMEBUFFER TRIAL", "cmdline=bosun-probe-cmdline.txt")):
        raise ValueError("Refusing to overwrite another tryboot configuration")
    for name in PROTECTED:
        if (boot / name).is_symlink() or not (boot / name).is_file():
            raise ValueError(f"Unexpected normal boot file: {name}")
    asset_hashes = {name: digest(build / name) for name in ASSET_FILES}
    fingerprint = {"assets": asset_hashes, "units": services("@ASSETS@", "@TOKEN@")}
    token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16]
    assets = f"/opt/bosun-hub/boot-splash-trials/{token}"
    trial_units = services(assets, token)
    cmdline_name = f"bosun-{token}.txt"
    cmdline = quiet_cmdline((boot / "cmdline.txt").read_text())
    tokens = [t for t in cmdline.split() if not t.startswith((
        "bosun.framebuffer_splash=", "bosun.splash_trial=", "bosun.tryboot_probe="))]
    tokens += ["bosun.framebuffer_splash=1", f"bosun.splash_trial={token}"]
    trial_cmdline = " ".join(tokens) + "\n"
    hashes = {name: digest(boot / name) for name in PROTECTED}
    backup = build / "known-good"
    backup.mkdir(exist_ok=False)
    for name in PROTECTED:
        shutil.copyfile(boot / name, backup / name)
    normal_paths = (["etc/systemd/system/" + name
                     for name in services("/opt/bosun-hub/boot-splash")] +
                    ["opt/bosun-hub/boot-splash/" + name for name in ASSET_FILES])
    normal_hashes = {}
    for name in normal_paths:
        path = root / name
        normal_hashes[name] = digest(path) if path.exists() else None
        if path.exists():
            saved = backup / "installed" / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    metadata = {"hashes": hashes, "asset_hashes": asset_hashes, "token": token,
                "cmdline": cmdline_name, "build": str(build),
                "trial_cmdline": trial_cmdline, "normal_hashes": normal_hashes,
                "trial_units": trial_units,
                "permanent_units": services("/opt/bosun-hub/boot-splash")}
    atomic_write(build / "trial.json", json.dumps(metadata, indent=2).encode())
    for name in ASSET_FILES:
        destination = root / assets.lstrip("/") / name
        atomic_write(destination, (build / name).read_bytes())
        if not name.endswith(".bin"): destination.chmod(0o755)
    for name, content in trial_units.items():
        atomic_write(root / "etc/systemd/system" / name, content.encode())
    atomic_write(boot / cmdline_name, trial_cmdline.encode())
    trial_config = config.rstrip() + (
        "\n\n# BOSUN FRAMEBUFFER TRIAL\n[all]\n" + f"cmdline={cmdline_name}\n")
    # Publish the selector last; the original kernel and initramfs are reused.
    atomic_write(trial, trial_config.encode())
    if hashes != {name: digest(boot / name) for name in PROTECTED}:
        raise ValueError("A normal boot file changed while staging the trial")
    return build / "trial.json"


def commit(root: Path, manifest: Path):
    root = root.resolve()
    state = json.loads(manifest.read_text())
    boot = root / "boot/firmware"
    build = Path(state["build"])
    running = (root / "proc/cmdline").read_text().split()
    if f"bosun.splash_trial={state['token']}" not in running:
        raise ValueError("Boot the candidate with tryboot before making it permanent")
    result = (root / "run/bosun-splash.result").read_text()
    if not result.startswith("Bosun splash drawn on "):
        raise ValueError("The boot trial did not draw the splash successfully")
    if (root / "run/bosun-splash.progress").read_text().strip() != "80":
        raise ValueError("The boot trial did not reach the final framebuffer milestone")
    if state["hashes"] != {name: digest(boot / name) for name in PROTECTED}:
        raise ValueError("Normal boot files changed since the trial was prepared")
    if (boot / state["cmdline"]).read_text() != state["trial_cmdline"]:
        raise ValueError("Candidate cmdline changed after validation")
    if state["normal_hashes"] != {name: digest(root / name) if (root / name).exists() else None
                                  for name in state["normal_hashes"]}:
        raise ValueError("Installed splash changed since the trial was prepared")
    trial_assets = root / "opt/bosun-hub/boot-splash-trials" / state["token"]
    if state["asset_hashes"] != {name: digest(trial_assets / name) for name in state["asset_hashes"]}:
        raise ValueError("Candidate files changed after validation")
    unit_directory = root / "etc/systemd/system"
    if any((unit_directory / name).read_text() != content
           for name, content in state["trial_units"].items()):
        raise ValueError("Candidate services changed after validation")
    if state["permanent_units"] != services("/opt/bosun-hub/boot-splash"):
        raise ValueError("Service configuration changed; prepare a new trial")
    assets = root / "opt/bosun-hub/boot-splash"
    assets.mkdir(parents=True, exist_ok=True)
    for name in state["asset_hashes"]:
        atomic_write(assets / name, (trial_assets / name).read_bytes())
        if not name.endswith(".bin"): (assets / name).chmod(0o755)
    for name, content in state["permanent_units"].items():
        atomic_write(unit_directory / name, content.encode())
    final_tokens = [t for t in (boot / state["cmdline"]).read_text().split()
                    if not t.startswith("bosun.splash_trial=")]
    atomic_write(boot / "cmdline.txt", (" ".join(final_tokens) + "\n").encode())
    return "Framebuffer splash enabled; known-good files remain in " + str(build / "known-good")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "commit"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run on the Pi with sudo")
    try:
        if args.action == "commit":
            for unit in ("bosun-hub", "bosun-kiosk", "bosun-midi.timer"):
                subprocess.run(["systemctl", "is-active", "--quiet", unit], check=True)
        print(stage(Path("/"), args.path) if args.action == "stage" else commit(Path("/"), args.path))
        if args.action == "commit":
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            state = json.loads(args.path.read_text())
            subprocess.run(["systemctl", "enable", *state["permanent_units"]], check=True)
            subprocess.run(["systemctl", "disable", *state["trial_units"]], check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Bosun splash: {error}\n")
