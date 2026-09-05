"""Static lifecycle contract for the systemd-managed Stage kiosk.

These checks prevent PAM/session-scope migration and stale Wayland runtime
state from silently returning. The process/cgroup behaviour itself is covered
by the real-appliance restart test documented alongside the assertions below.
"""

from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
KIOSK_UNIT = ROOT / "systemd" / "bosun-kiosk.service"
WAYVNC_UNIT = ROOT / "systemd" / "bosun-wayvnc.service"
LAUNCHER = ROOT / "kiosk" / "bosun-kiosk.sh"
INSTALLER = ROOT / "install.sh"


def _active_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _value(lines: list[str], directive: str) -> str:
    prefix = directive + "="
    matches = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    assert len(matches) == 1, f"expected one {directive}= directive, got {matches}"
    return matches[0]


def _positions(text: str, *needles: str) -> list[int]:
    positions = [text.find(needle) for needle in needles]
    assert all(position >= 0 for position in positions), dict(zip(needles, positions))
    return positions


def test_kiosk_stays_in_service_cgroup_and_has_bounded_stop():
    lines = _active_lines(KIOSK_UNIT)

    # PAM's pam_systemd module creates a login session scope and migrates the
    # process tree out of bosun-kiosk.service, making TasksCurrent misleading
    # and control-group cleanup incomplete.
    assert not any(line.startswith("PAMName=") for line in lines)
    assert "seatd.service" in _value(lines, "Wants").split()
    assert "seatd.service" in _value(lines, "After").split()
    assert _value(lines, "KillMode") == "control-group"
    assert _value(lines, "TimeoutStopSec") == "10s"
    assert _value(lines, "RuntimeDirectory") == "bosun-kiosk"
    assert _value(lines, "RuntimeDirectoryMode") == "0700"
    assert not any(line.startswith("RuntimeDirectoryPreserve=") for line in lines)

    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert re.search(r"(?m)^exec cage -- ", launcher), "cage must remain MainPID"
    assert not re.search(r"\b(?:setsid|nohup|systemd-run)\b", launcher)


def test_chromium_profile_is_persistent_systemd_state_with_no_home_fallback():
    lines = _active_lines(KIOSK_UNIT)

    # This reproduces the appliance regression statically: a bare
    # --user-data-dir under /var/lib is not enough because Chromium silently
    # falls back to ~/.config when the parent is absent or unusable.  The unit
    # must ask PID 1 to create the exact persistent directory for User=bosun.
    assert _value(lines, "User") == "bosun"
    assert _value(lines, "StateDirectory") == "bosun-hub/chromium"
    assert _value(lines, "StateDirectoryMode") == "0700"

    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert 'CHROMIUM_PROFILE="${STATE_DIRECTORY:-}"' in launcher
    assert 'EXPECTED_CHROMIUM_PROFILE=/var/lib/bosun-hub/chromium' in launcher
    for guard in (
        '! -d "$CHROMIUM_PROFILE"',
        '-L "$CHROMIUM_PROFILE"',
        '! -O "$CHROMIUM_PROFILE"',
        '! -w "$CHROMIUM_PROFILE"',
    ):
        assert guard in launcher
    assert '--user-data-dir="$CHROMIUM_PROFILE"' in launcher
    assert '--user-data-dir=/var/lib/bosun-hub/chromium' not in launcher
    active_launcher = "\n".join(
        line for line in launcher.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert not re.search(
        r'(?:\$\{?HOME\}?|~/|/home/bosun|\.config)', active_launcher
    )


def test_kiosk_disables_continuous_stage_animations():
    launcher = LAUNCHER.read_text(encoding="utf-8")

    # The Stage keeps active switches visually distinct without motion.  On
    # the RPi3, its infinite transform/filter animations otherwise keep the
    # Chromium renderer and GPU busy even while the rig is completely idle.
    assert "--force-prefers-reduced-motion" in launcher


def test_kiosk_bypasses_rpi_desktop_chromium_flags():
    launcher = LAUNCHER.read_text(encoding="utf-8")

    # The Raspberry Pi OS wrapper sources /etc/chromium.d/00-rpi-vars, which
    # forces accessibility and extension WebUI processes intended for a full
    # desktop session.  The appliance launches the same packaged binary with
    # an explicit, minimal set of graphics flags.
    assert "[[ -x /usr/lib/chromium/chromium ]]" in launcher
    assert "CHROMIUM=/usr/lib/chromium/chromium" in launcher
    assert "--disable-background-networking" in launcher
    assert "--disable-extensions" in launcher
    assert "--enable-gpu-rasterization" in launcher
    assert "--use-angle=gles" in launcher


def test_launcher_fails_closed_when_systemd_state_is_absent():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required for the kiosk launcher contract test")

    environment = os.environ.copy()
    environment.pop("STATE_DIRECTORY", None)
    completed = subprocess.run(
        [bash, "kiosk/bosun-kiosk.sh"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 1
    assert "state directory is missing, unsafe, or not writable: <unset>" in completed.stderr
    assert "cage/chromium missing" not in completed.stderr


def test_installer_verifies_the_systemd_managed_profile_after_restart():
    installer = INSTALLER.read_text(encoding="utf-8")
    unit_install, daemon_reload, kiosk_restart, active_check, profile_check = _positions(
        installer,
        'install -m 644 "$SRC"/systemd/bosun-kiosk.service',
        "systemctl daemon-reload",
        "systemctl restart bosun-kiosk.service",
        "systemctl is-active --quiet bosun-kiosk.service",
        'stat -c %U /var/lib/bosun-hub/chromium',
    )
    assert unit_install < daemon_reload < kiosk_restart < active_check < profile_check
    assert 'test ! -L /var/lib/bosun-hub/chromium' in installer
    assert 'runuser -u "$HUB_USER" -- test -w /var/lib/bosun-hub/chromium' in installer
    assert '"$HUB_USER"' in installer[profile_check:]


def test_installer_fails_if_hub_is_not_active_after_all_restarts():
    installer = INSTALLER.read_text(encoding="utf-8")
    hub_restart, kiosk_restart, hub_check, final_status = _positions(
        installer,
        "systemctl restart bosun-hub.service",
        "systemctl restart bosun-kiosk.service",
        "if ! systemctl is-active --quiet bosun-hub.service; then",
        "systemctl --no-pager --lines=0 status bosun-hub.service || true",
    )

    # `systemctl restart` only proves that the start job was accepted.  A
    # Type=simple process can die immediately afterwards, so the installer
    # must make a final inactive state fatal instead of masking `status` with
    # `|| true` and printing a misleading success message.
    assert hub_restart < hub_check < final_status
    assert kiosk_restart < hub_check
    fatal_block = re.search(
        r"if ! systemctl is-active --quiet bosun-hub\.service; then"
        r"(?P<body>.*?)\nfi",
        installer,
        re.DOTALL,
    )
    assert fatal_block, "hub activity check must have an explicit failure path"
    assert "exit 1" in fatal_block.group("body")
    assert "status bosun-hub.service" in fatal_block.group("body")

    bash = shutil.which("bash")
    if bash is None:
        return

    guard = fatal_block.group(0)
    for active, expected_code in ((True, 0), (False, 1)):
        mock = f"""
systemctl() {{
    case "$*" in
        'is-active --quiet bosun-hub.service')
            {'return 0' if active else 'return 3'}
            ;;
        '--no-pager --lines=30 status bosun-hub.service')
            printf 'mock failed status\\n' >&2
            return 3
            ;;
        *)
            return 97
            ;;
    esac
}}
{guard}
"""
        completed = subprocess.run(
            [bash],
            # Binary stdin avoids Windows' text-mode CRLF translation before
            # the script reaches WSL/Git Bash.
            input=mock.encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )
        stderr = completed.stderr.decode("utf-8", errors="replace")
        assert completed.returncode == expected_code, stderr
        if not active:
            assert "bosun-hub.service is not active after restart" in stderr
            assert "mock failed status" in stderr


def test_wayvnc_uses_the_same_ephemeral_private_runtime():
    lines = _active_lines(WAYVNC_UNIT)
    assert _value(lines, "RuntimeDirectory") == "bosun-kiosk"
    assert _value(lines, "RuntimeDirectoryMode") == "0700"
    assert _value(lines, "Environment") == "XDG_RUNTIME_DIR=/run/bosun-kiosk"
    assert not any(line.startswith("RuntimeDirectoryPreserve=") for line in lines)


def test_installer_repairs_groups_for_new_and_existing_users():
    installer = INSTALLER.read_text(encoding="utf-8")
    loop = re.search(
        r"for group in (?P<groups>[^;\n]+); do(?P<body>.*?)\ndone",
        installer,
        re.DOTALL,
    )
    assert loop, "installer must reconcile appliance groups on every run"
    groups = set(loop.group("groups").split())
    assert {"audio", "video", "input", "render", "plugdev", "seat"} <= groups
    assert 'getent group "$group"' in loop.group("body")
    assert 'usermod --append --groups "$group" "$HUB_USER"' in loop.group("body")
