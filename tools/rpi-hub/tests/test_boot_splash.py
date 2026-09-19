"""Protect the Pi's boot parameters and deployed Stage while adding branding."""
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "boot-splash/configure.py"
spec = importlib.util.spec_from_file_location("boot_splash", SOURCE)
splash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splash)


@pytest.fixture
def appliance(tmp_path):
    root = tmp_path / "pi"
    boot = root / "boot/firmware"
    boot.mkdir(parents=True)
    (boot / "config.txt").write_text("[all]\ndtoverlay=vc4-kms-v3d\n[pi5]\ndtoverlay=nospi10\n")
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 "
        "fsck.repair=yes rootwait ds=nocloud;i=123 cfg80211.ieee80211_regdom=IT"
    )
    stage = root / "opt/bosun-hub/stage"
    stage.mkdir(parents=True)
    (stage / "index.html").write_text(
        '<html><head><script type="module" src="/assets/installed.js"></script></head>'
        '<body><div id="app"></div></body></html>'
    )
    return root


def test_keeps_root_hardware_serial_console_and_deployed_bundle(appliance):
    boot = appliance / "boot/firmware"
    original = (boot / "cmdline.txt").read_bytes()
    version = splash.configure(appliance)
    tokens = (boot / "cmdline.txt").read_text().split()
    for token in original.decode().split():
        if token != "console=tty1":
            assert token in tokens
    assert "console=tty1" not in tokens
    assert "console=tty3" in tokens
    assert "loglevel=0" in tokens
    assert all(token in tokens for token in splash.BOOT_OPTIONS)
    assert (boot / "cmdline.txt.bosun-before-splash").read_bytes() == original
    config = (boot / "config.txt").read_text()
    assert config.startswith("[all]\ndtoverlay=vc4-kms-v3d\n[pi5]\ndtoverlay=nospi10\n")
    assert "[all]\ndisable_splash=1" in config
    assert "auto_initramfs=" not in config
    assert "plymouth.enable=0" in tokens
    assert "nosplash" in tokens
    assert "splash" not in tokens
    assert "bosun.quiet=1" in tokens
    assert "bosun.splash=1" not in tokens
    page = (appliance / "opt/bosun-hub/stage/index.html").read_text()
    assert 'src="/assets/installed.js"' in page
    assert 'id="app"' in page
    assert f"v{version}" in page
    assert "__BOSUN_" not in page
    assert "data:image/png;base64," in page
    assert not (appliance / "usr/share/plymouth").exists()


def test_quiet_boot_does_not_add_a_display_service(appliance):
    splash.configure(appliance)
    units = appliance / "etc/systemd/system"
    assert not (units / "bosun-boot-splash.service").exists()
    assert not (units / "bosun-kiosk.service.d").exists()
    assert "ConditionKernelCommandLine=!bosun.quiet=1" in (
        units / "getty@tty1.service.d/50-bosun-splash.conf").read_text()


def test_removes_legacy_splash_startup_without_reenabling_it(appliance):
    units = appliance / "etc/systemd/system"
    for name, signature in splash.OBSOLETE_UNITS.items():
        path = units / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(signature)
    boot = appliance / "boot/firmware/cmdline.txt"
    boot.write_text(boot.read_text() + " bosun.splash=1 splash plymouth.enable=1")
    splash.configure(appliance)
    assert all(not (units / name).exists() for name in splash.OBSOLETE_UNITS)
    tokens = boot.read_text().split()
    assert "bosun.splash=1" not in tokens
    assert "plymouth.enable=0" in tokens
    assert "splash" not in tokens


def test_modified_legacy_unit_is_preserved_and_fails_before_changes(appliance):
    unit = appliance / "etc/systemd/system/bosun-boot-splash.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/custom-program\n")
    before = {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="was modified"):
        splash.configure(appliance)
    assert {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()} == before


def test_rerun_keeps_original_backups_and_does_not_duplicate_settings(appliance):
    splash.configure(appliance)
    boot = appliance / "boot/firmware"
    backup = (boot / "config.txt.bosun-before-splash").read_bytes()
    cmdline = (boot / "cmdline.txt").read_bytes()
    page_before = (appliance / "opt/bosun-hub/stage/index.html").read_bytes()
    splash.configure(appliance)
    assert (boot / "config.txt.bosun-before-splash").read_bytes() == backup
    assert (boot / "cmdline.txt").read_bytes() == cmdline
    assert (appliance / "opt/bosun-hub/stage/index.html").read_bytes() == page_before
    assert (boot / "config.txt").read_text().count(splash.BEGIN) == 1
    page = (appliance / "opt/bosun-hub/stage/index.html").read_text()
    assert page.count('id="bosun-boot-splash"') == 1


@pytest.mark.parametrize("bad", ["console=tty1", "root=a\nquiet", "root=a root=b"])
def test_invalid_boot_input_fails_before_any_file_is_changed(appliance, bad):
    (appliance / "boot/firmware/cmdline.txt").write_text(bad)
    before = {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        splash.configure(appliance)
    assert {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()} == before


def test_preflight_does_not_write_anything(appliance):
    before = {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()}
    splash.configure(appliance, check=True)
    assert {p: p.read_bytes() for p in appliance.rglob("*") if p.is_file()} == before


def test_refresh_uses_new_package_version_without_replacing_stage_js(appliance, tmp_path):
    repo = tmp_path / "checkout"
    (repo / "editor/src-tauri/icons").mkdir(parents=True)
    (repo / "editor/src-tauri/icons/icon.png").write_bytes(
        (splash.REPO / "editor/src-tauri/icons/icon.png").read_bytes())
    (repo / "editor/stage-kiosk.html").write_bytes((splash.REPO / "editor/stage-kiosk.html").read_bytes())
    (repo / "editor/package.json").write_text(json.dumps({"version": "9.8.7"}))
    splash.configure(appliance)
    splash.configure(appliance, repo=repo)
    page = (appliance / "opt/bosun-hub/stage/index.html").read_text()
    assert "v9.8.7" in page
    assert 'src="/assets/installed.js"' in page
