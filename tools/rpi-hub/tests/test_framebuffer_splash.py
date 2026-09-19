"""Exercise trial isolation and refusal to activate an unvalidated boot image."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "boot-splash"
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location("framebuffer_trial", SOURCE / "trial.py")
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)
sys.path.pop(0)


@pytest.fixture
def staged_system(tmp_path):
    root, build = tmp_path / "pi", tmp_path / "build"
    boot = root / "boot/firmware"
    boot.mkdir(parents=True)
    build.mkdir()
    for name in trial.PROTECTED:
        (boot / name).write_bytes(b"existing working file")
    (boot / "config.txt").write_text("auto_initramfs=1\ndtoverlay=vc4-kms-v3d\n")
    (boot / "cmdline.txt").write_text("console=tty3 root=PARTUUID=abcd-02 rootwait quiet plymouth.enable=0\n")
    (build / "bosun-boot-picture").write_bytes(b"renderer")
    (build / "splash.bin").write_bytes(b"picture")
    for percent in (45, 65, 80):
        (build / f"splash-{percent}.bin").write_bytes(f"picture {percent}".encode())
    (build / "show.sh").write_bytes(b"#!/bin/sh\n")
    return root, build


def test_trial_never_changes_the_normal_boot_files(staged_system):
    root, build = staged_system
    boot = root / "boot/firmware"
    before = {name: (boot / name).read_bytes() for name in trial.PROTECTED}
    manifest = trial.stage(root, build)
    state = json.loads(manifest.read_text())
    assert before == {name: (boot / name).read_bytes() for name in trial.PROTECTED}
    assert before == {name: (build / "known-good" / name).read_bytes() for name in trial.PROTECTED}
    assert f"cmdline={state['cmdline']}" in (boot / "tryboot.txt").read_text()
    command = (boot / state["cmdline"]).read_text().split()
    assert "root=PARTUUID=abcd-02" in command
    assert "plymouth.enable=0" in command
    assert "bosun.framebuffer_splash=1" in command
    assert not any(t.startswith("fullscreen_logo") for t in command)


def test_another_tryboot_setup_is_not_overwritten(staged_system):
    root, build = staged_system
    boot = root / "boot/firmware"
    (boot / "tryboot.txt").write_text("kernel=someone-elses.img\n")
    before = {p.name: p.read_bytes() for p in boot.iterdir()}
    with pytest.raises(ValueError, match="another tryboot"):
        trial.stage(root, build)
    assert before == {p.name: p.read_bytes() for p in boot.iterdir()}


def test_commit_requires_the_exact_successful_running_trial(staged_system):
    root, build = staged_system
    manifest = trial.stage(root, build)
    (root / "proc").mkdir()
    (root / "proc/cmdline").write_text("root=PARTUUID=abcd-02")
    before = (root / "boot/firmware/initramfs8").read_bytes()
    with pytest.raises(ValueError, match="Boot the candidate"):
        trial.commit(root, manifest)
    assert (root / "boot/firmware/initramfs8").read_bytes() == before


def test_successful_commit_keeps_kernel_config_and_initramfs(staged_system):
    root, build = staged_system
    boot = root / "boot/firmware"
    manifest = trial.stage(root, build)
    state = json.loads(manifest.read_text())
    (root / "proc").mkdir()
    (root / "proc/cmdline").write_text((boot / state["cmdline"]).read_text())
    (root / "run").mkdir()
    (root / "run/bosun-splash.result").write_text("Bosun splash drawn on 1920x440 framebuffer\n")
    (root / "run/bosun-splash.progress").write_text("80\n")
    unchanged = {name: (boot / name).read_bytes() for name in trial.PROTECTED if name != "cmdline.txt"}
    trial.commit(root, manifest)
    assert unchanged == {name: (boot / name).read_bytes() for name in unchanged}
    assert "bosun.framebuffer_splash=1" in (boot / "cmdline.txt").read_text()
    assert "bosun.splash_trial=" not in (boot / "cmdline.txt").read_text()
    service = (root / "etc/systemd/system/bosun-framebuffer-splash.service").read_text()
    assert "TimeoutStartSec=3" in service
    assert "ConditionKernelCommandLine=!bosun.splash_trial" in service


def test_commit_refuses_changed_baseline_or_candidate(staged_system):
    root, build = staged_system
    boot = root / "boot/firmware"
    manifest = trial.stage(root, build)
    state = json.loads(manifest.read_text())
    (root / "proc").mkdir()
    (root / "proc/cmdline").write_text((boot / state["cmdline"]).read_text())
    (root / "run").mkdir()
    (root / "run/bosun-splash.result").write_text("Bosun splash drawn on 1920x440 framebuffer\n")
    (root / "run/bosun-splash.progress").write_text("80\n")
    (boot / "kernel8.img").write_bytes(b"a newer kernel")
    with pytest.raises(ValueError, match="changed since"):
        trial.commit(root, manifest)
    assert not (root / "opt/bosun-hub/boot-splash").exists()


@pytest.mark.parametrize("changed", ["picture", "service", "incomplete", "cmdline", "normal"])
def test_commit_refuses_unverified_progress(staged_system, changed):
    root, build = staged_system
    manifest = trial.stage(root, build)
    state = json.loads(manifest.read_text())
    (root / "proc").mkdir()
    (root / "proc/cmdline").write_text((root / "boot/firmware" / state["cmdline"]).read_text())
    (root / "run").mkdir()
    (root / "run/bosun-splash.result").write_text("Bosun splash drawn on 1920x440 framebuffer\n")
    (root / "run/bosun-splash.progress").write_text("45" if changed == "incomplete" else "80")
    if changed == "picture":
        (root / "opt/bosun-hub/boot-splash-trials" / state["token"] / "splash-80.bin").write_bytes(b"changed")
    if changed == "service":
        (root / "etc/systemd/system/bosun-framebuffer-splash-trial-80.service").write_text("changed")
    if changed == "cmdline":
        (root / "boot/firmware" / state["cmdline"]).write_text("root=unexpected")
    if changed == "normal":
        (root / "etc/systemd/system/bosun-framebuffer-splash.service").write_text("changed")
    before = {name: (root / "boot/firmware" / name).read_bytes() for name in trial.PROTECTED}
    with pytest.raises(ValueError, match="changed|milestone"):
        trial.commit(root, manifest)
    assert before == {name: (root / "boot/firmware" / name).read_bytes() for name in trial.PROTECTED}
    assert not (root / "opt/bosun-hub/boot-splash").exists()
