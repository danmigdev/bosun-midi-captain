"""Run the real appliance installers offline with isolated files and fake OS tools."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1]


def shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


FAKE_TOOL = r'''#!/usr/bin/env bash
set -eu
name="${0##*/}"
printf '%s' "$name" >> "$EVENT_LOG"
printf ' %s' "$@" >> "$EVENT_LOG"
printf '\n' >> "$EVENT_LOG"
safe_destination() {
    case "$1" in "$APPLIANCE_TEST_ROOT"/*) ;; *) echo 'unsafe test destination' >&2; exit 90;; esac
}
case "$name" in
apt-get|usermod|chown|udevadm) ;;
dpkg-query)
    if [[ "${PACKAGES_PRESENT:-0}" == 1 && "${!#}" != "${MISSING_PACKAGE:-}" ]]; then printf installed; fi
    ;;
id) [[ "${EXISTING_USER:-1}" == 1 || -f "$APPLIANCE_TEST_ROOT/user-created" ]];;
useradd) touch "$APPLIANCE_TEST_ROOT/user-created";;
getent) [[ "${2:-}" != seat ]];;
rsync)
    sources=()
    while (($#)); do
        case "$1" in --exclude) shift 2;; -*) shift;; *) sources+=("$1"); shift;; esac
    done
    destination="${sources[-1]}"
    safe_destination "$destination"
    mkdir -p "$destination"
    for ((i=0; i<${#sources[@]}-1; i++)); do
        source="${sources[i]}"
        if [[ "$source" == */ ]]; then
            cp -a "$source." "$destination/"
        else
            cp -a "$source" "$destination/"
        fi
    done
    ;;
install)
    destination="${!#}"
    safe_destination "$destination"
    if [[ "$1" == -d ]]; then
        mkdir -p "$destination"
    else
        source="${@: -2:1}"
        cp "$source" "$destination"
    fi
    ;;
picotool)
    echo 'installer attempted to access a Captain' >&2
    exit 99
    ;;
systemctl)
    if [[ "$1" == restart && "${2:-}" == bosun-kiosk.service ]]; then
        mkdir -p "$APPLIANCE_TEST_ROOT/system/var/lib/bosun-hub/chromium"
    fi
    if [[ "$1" == is-active && "${!#}" == "${FAIL_SERVICE:-}" ]]; then exit 3; fi
    if [[ "$1" == restart && "${2:-}" == "${FAIL_RESTART:-}" ]]; then exit 1; fi
    ;;
python3)
    # Quiet-boot configuration is covered by test_boot_splash.py; only fake
    # the picture renderer's outputs here.
    if [[ "$1" == */render.py ]]; then
        for picture in splash splash-45 splash-65 splash-80; do
            printf 'new %s\n' "$picture" > "$(dirname "$2")/$picture.bin"
        done
    fi
    ;;
stat) printf 'bosun\n';;
runuser) [[ "${PROFILE_WRITABLE:-1}" == 1 ]];;
*) echo 'unhandled fake command' >&2; exit 98;;
esac
'''


@pytest.fixture
def appliance(tmp_path):
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is required to execute the appliance installer")
    checkout = tmp_path / "checkout"
    source = checkout / "tools/rpi-hub"
    source.mkdir(parents=True)
    system = tmp_path / "system"
    for path in ("etc/systemd/system", "etc/udev/rules.d", "opt", "var/lib"):
        (system / path).mkdir(parents=True, exist_ok=True)
    for filename in ("install.sh", "install-native-updater.sh"):
        script = (SOURCE / filename).read_text(encoding="utf-8")
        # Execute the installer logic, but give it a simulated root identity and
        # redirect every fixed installation destination into this temporary tree.
        script = script.replace("$EUID", "0")
        # Git Bash prepends /usr/bin when starting; put the interceptors first
        # inside each shell as well, including the nested updater installer.
        script = script.replace(
            "set -euo pipefail\n",
            f'set -euo pipefail\nexport PATH="{shell_path(tmp_path / "fake-bin")}:$PATH"\n',
            1,
        )
        for prefix in ("/opt/bosun-hub", "/etc/systemd/system", "/etc/udev/rules.d", "/var/lib/bosun-hub",
                       "/usr/local/libexec", "/etc/bosun"):
            script = script.replace(prefix, shell_path(system) + prefix)
        (source / filename).write_text(script, encoding="utf-8", newline="\n")
    shutil.copytree(SOURCE / "systemd", source / "systemd")
    shutil.copytree(SOURCE / "udev", source / "udev")
    shutil.copytree(SOURCE / "kiosk", source / "kiosk")
    shutil.copytree(SOURCE / "pi-update", source / "pi-update", ignore=shutil.ignore_patterns("__pycache__"))
    (source / "allowed_signers").write_text('bosun-release namespaces="bosun-pi-package" ssh-ed25519 AAAA test\n')
    (source / "bosun_hub").mkdir()
    (source / "bosun_hub/__main__.py").write_text("# installed module fixture\n")
    (source / "requirements.txt").write_text("# offline fixture\n")
    (source / "README.md").write_text("Offline installer fixture.\n")
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    for name in (
        "apt-get", "id", "useradd", "getent", "usermod", "rsync", "install",
        "chown", "picotool", "udevadm", "systemctl", "stat", "runuser", "dpkg-query",
    ):
        command = binary / name
        command.write_text(FAKE_TOOL, encoding="utf-8", newline="\n")
        command.chmod(0o755)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    event_log = tmp_path / "events.log"
    environment = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        "APPLIANCE_TEST_ROOT": shell_path(tmp_path),
        "EVENT_LOG": shell_path(event_log),
        "TMPDIR": shell_path(temporary),
    }

    class Appliance:
        stage = checkout / "editor/dist-stage"
        installed_stage = system / "opt/bosun-hub/stage"

        def build_stage(self, entry="stage-kiosk.html"):
            self.stage.mkdir(parents=True, exist_ok=True)
            (self.stage / entry).write_text("<p>new Stage</p>\n")

        def run(self, **overrides):
            result = subprocess.run(
                [bash, "--noprofile", "--norc", shell_path(source / "install.sh")],
                env={**environment, **overrides}, text=True, capture_output=True, timeout=30,
            )
            events = event_log.read_text().splitlines() if event_log.exists() else []
            assert not any(line.startswith("picotool ") or line == "picotool" for line in events)
            return result, events

    instance = Appliance()
    instance.system = system
    instance.source = source
    return instance


@pytest.mark.parametrize("missing", ["stage", "kiosk/bosun-hdmi-recovery.py", "udev/60-bosun-update.rules",
                                     "pi-update/install_package.py", "systemd/bosun-pi-update.path"])
def test_missing_build_or_checkout_fails_before_any_appliance_mutation(appliance, missing):
    if missing != "stage":
        appliance.build_stage()
        (appliance.source / missing).unlink()
    result, events = appliance.run()
    assert result.returncode != 0
    assert events == []


@pytest.mark.parametrize("entry", ["stage-kiosk.html", "index.html"])
def test_fresh_install_sets_access_and_native_support_before_starting_services(appliance, entry):
    appliance.build_stage(entry)
    result, events = appliance.run(EXISTING_USER="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (appliance.installed_stage / "index.html").read_text() == "<p>new Stage</p>\n"
    assert (appliance.system / "etc/udev/rules.d/60-bosun-update.rules").is_file()
    assert (appliance.system / "etc/systemd/system/bosun-hub.service.d/update-timeout.conf").read_text() == (
        "[Service]\nTimeoutStopSec=3600\n")
    assert (appliance.system / "etc/udev/rules.d/99-bosun-kiosk-input.rules").is_file()
    first_restart = next(i for i, line in enumerate(events) if line.startswith("systemctl restart "))
    before_start = events[:first_restart]
    assert before_start.index("udevadm control --reload") < before_start.index(
        "udevadm trigger --action=change --subsystem-match=input"
    ) < before_start.index("udevadm settle --timeout=10")
    assert any(line.startswith("apt-get install ") and "picotool" in line and "seatd" in line
               for line in before_start)
    for group in ("audio", "video", "input", "render", "plugdev", "dialout"):
        assert f"usermod --append --groups {group} bosun" in before_start
    assert "systemctl enable --now seatd.service" in before_start
    assert any(line.startswith("systemctl enable ") and "bosun-wayvnc.service" in line for line in events)


def test_existing_stage_is_preserved_without_replacement_build(appliance):
    appliance.installed_stage.mkdir(parents=True)
    (appliance.installed_stage / "index.html").write_text("existing Stage\n")
    (appliance.installed_stage / "retained.js").write_text("existing bundle\n")
    result, _ = appliance.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (appliance.installed_stage / "index.html").read_text() == "existing Stage\n"
    assert (appliance.installed_stage / "retained.js").read_text() == "existing bundle\n"


@pytest.mark.parametrize("failed_service", ["bosun-hub.service", "bosun-kiosk.service"])
def test_inactive_service_is_reported_as_failed_install(appliance, failed_service):
    appliance.build_stage()
    result, _ = appliance.run(FAIL_SERVICE=failed_service)
    assert result.returncode != 0


def test_install_hands_only_stage_to_the_hub_account(appliance):
    appliance.build_stage()
    # Left by an earlier installer that ran chown -R bosun:bosun /opt/bosun-hub.
    (appliance.system / "opt/bosun-hub/boot-splash").mkdir(parents=True)
    result, events = appliance.run()
    assert result.returncode == 0, result.stdout + result.stderr
    dest = shell_path(appliance.system) + "/opt/bosun-hub"
    first_restart = next(i for i, line in enumerate(events) if line.startswith("systemctl restart "))
    chowns = [line for line in events[:first_restart] if line.startswith("chown ")]
    assert f"chown root:root {dest}" in chowns
    assert [line for line in events if line.startswith("chown -R bosun:bosun")] == [
        f"chown -R bosun:bosun {dest}/stage"]
    root_owned = " ".join(line for line in chowns if line.startswith("chown -R root:root ")).split()
    for name in ("bosun_hub", "kiosk", "README.md", "requirements.txt", "boot-splash"):
        assert f"{dest}/{name}" in root_owned
    assert f"{dest}/stage" not in root_owned
    unit = (appliance.system / "etc/systemd/system/bosun-hub.service").read_text()
    assert "StateDirectory=bosun-hub/boot-logo" in unit
    assert "--boot-logo-dir /var/lib/bosun-hub/boot-logo" in unit


def test_present_packages_need_no_network(appliance):
    appliance.build_stage()
    result, events = appliance.run(PACKAGES_PRESENT="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipping apt-get" in result.stdout
    assert not any(line.startswith("apt-get") for line in events)
    assert any(line.startswith("dpkg-query -W ") and line.endswith(" openssh-client") for line in events)


def test_a_missing_package_installs_the_full_list(appliance):
    appliance.build_stage()
    result, events = appliance.run(PACKAGES_PRESENT="1", MISSING_PACKAGE="openssh-client")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installing: openssh-client" in result.stdout
    apt = [line for line in events if line.startswith("apt-get")]
    assert apt[0] == "apt-get update -qq"
    assert apt[1].startswith("apt-get install -y -qq ") and "picotool" in apt[1] and "openssh-client" in apt[1]


def test_install_sets_up_the_signed_pi_self_update(appliance):
    appliance.build_stage()
    result, events = appliance.run()
    assert result.returncode == 0, result.stdout + result.stderr
    system, root = appliance.system, shell_path(appliance.system)
    assert (system / "etc/bosun/allowed_signers").read_bytes() == (appliance.source / "allowed_signers").read_bytes()
    assert (system / "usr/local/libexec/bosun-pi-update").read_bytes() == (
        SOURCE / "pi-update/install_package.py").read_bytes()
    for name in ("bosun-pi-update.path", "bosun-pi-update.service"):
        assert (system / "etc/systemd/system" / name).read_bytes() == (SOURCE / "systemd" / name).read_bytes()
    assert any(line.startswith("install -o root -g root -m 0644 ") and
               line.endswith(f"{root}/etc/bosun/allowed_signers") for line in events)
    assert any(line.startswith("install -o root -g root -m 0755 ") and
               line.endswith(f"{root}/usr/local/libexec/bosun-pi-update") for line in events)
    assert events.index("systemctl daemon-reload") < events.index("systemctl enable --now bosun-pi-update.path")
    # The installer may run inside bosun-pi-update.service: only enable that unit.
    assert [line for line in events if "bosun-pi-update.service" in line and line.startswith("systemctl")] == [
        "systemctl enable bosun-pi-update.service"]
    unit = (system / "etc/systemd/system/bosun-hub.service").read_text()
    assert "StateDirectory=bosun-hub/pi-update" in unit


def test_checkout_without_a_release_key_installs_without_self_update(appliance):
    appliance.build_stage()
    (appliance.source / "allowed_signers").unlink()
    result, _ = appliance.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Desktop cannot update this Pi" in result.stderr
    assert not (appliance.system / "etc/bosun/allowed_signers").exists()


def test_no_installer_script_stops_or_restarts_the_running_self_update():
    for name in ("install.sh", "quick-install.sh", "install-native-updater.sh", "install-boot-splash.sh"):
        lines = [line.strip() for line in (SOURCE / name).read_text(encoding="utf-8").splitlines()
                 if re.search(r"\bsystemctl\b", line) and "bosun-pi-update" in line
                 and not line.strip().startswith("#")]
        assert lines in ([], ["systemctl enable bosun-pi-update.service",
                              "systemctl enable --now bosun-pi-update.path"]), name


@pytest.fixture
def splash_installer(appliance, tmp_path):
    """install-boot-splash.sh on an installed appliance, with fake OS tools."""
    system, source = appliance.system, appliance.source
    script = (SOURCE / "install-boot-splash.sh").read_text(encoding="utf-8").replace("$EUID", "0")
    script = script.replace(
        "set -euo pipefail\n",
        f'set -euo pipefail\nexport PATH="{shell_path(tmp_path / "fake-bin")}:$PATH"\n', 1)
    for prefix in ("/opt/bosun-hub", "/etc/systemd/system", "/usr/local/libexec", "/proc/device-tree/model"):
        script = script.replace(prefix, shell_path(system) + prefix)
    (source / "install-boot-splash.sh").write_text(script, encoding="utf-8", newline="\n")
    shutil.copytree(SOURCE / "boot-splash", source / "boot-splash",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "fake-bin/python3").write_text(FAKE_TOOL, encoding="utf-8", newline="\n")
    (tmp_path / "fake-bin/python3").chmod(0o755)
    (system / "proc/device-tree").mkdir(parents=True)
    (system / "proc/device-tree/model").write_text("Raspberry Pi 3 Model B Rev 1.2")
    (system / "etc/systemd/system/bosun-kiosk.service").write_text("[Service]\n")
    (system / "opt/bosun-hub").mkdir(parents=True)
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
    events_log = tmp_path / "events.log"

    def run(**overrides):
        environment = {
            **os.environ, "PATH": str(tmp_path / "fake-bin") + os.pathsep + os.environ.get("PATH", ""),
            "APPLIANCE_TEST_ROOT": shell_path(tmp_path), "EVENT_LOG": shell_path(events_log),
            "TMPDIR": shell_path(tmp_path / "temporary"), **overrides,
        }
        result = subprocess.run([bash, "--noprofile", "--norc", shell_path(source / "install-boot-splash.sh")],
                                env=environment, text=True, capture_output=True, timeout=30)
        return result, events_log.read_text().splitlines() if events_log.exists() else []

    return SimpleNamespace(system=system, run=run)


def test_splash_installer_reapplies_the_logo_after_refreshing_pictures(splash_installer):
    system = splash_installer.system
    units = system / "etc/systemd/system"
    for name in ("bosun-framebuffer-splash.service", "bosun-framebuffer-splash-80.service"):
        (units / name).write_text("[Service]\n")
    (system / "opt/bosun-hub/boot-splash").mkdir()
    result, events = splash_installer.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (system / "usr/local/libexec/bosun-boot-logo").read_bytes() == (
        SOURCE / "boot-splash/apply_logo.py").read_bytes()
    for name in ("bosun-boot-logo.service", "bosun-boot-logo.path"):
        assert (units / name).read_bytes() == (SOURCE / "systemd" / name).read_bytes()
    root = shell_path(system)
    assert any(line.startswith("install -o root -g root -m 0755 ") and
               line.endswith(f"{root}/usr/local/libexec/bosun-boot-logo") for line in events)
    assert f"chown root:root {root}/opt/bosun-hub" in events
    assert f"chown -R root:root {root}/opt/bosun-hub/boot-splash" in events
    reload, stop, enable, restart = (events.index(line) for line in (
        "systemctl daemon-reload",
        "systemctl stop bosun-boot-logo.path bosun-boot-logo.service",
        "systemctl enable --now bosun-boot-logo.path",
        "systemctl restart bosun-boot-logo.service",
    ))
    render = next(i for i, line in enumerate(events) if "render.py" in line)
    # No applier run can overwrite a picture while the refresh replaces it,
    # and the refreshed pictures get the custom logo again afterwards.
    assert reload < stop < render < enable < restart
    for picture in ("splash", "splash-45", "splash-65", "splash-80"):
        assert (system / f"opt/bosun-hub/boot-splash/{picture}.bin").read_text() == f"new {picture}\n"
    assert not list((system / "opt/bosun-hub/boot-splash").glob(".pictures.*"))


def test_splash_installer_sets_up_the_applier_without_framebuffer_pictures(splash_installer):
    result, events = splash_installer.run(FAIL_RESTART="bosun-boot-logo.service")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "custom startup logo was not applied" in result.stderr
    assert "display stays black until Chromium starts" in result.stdout
    assert not any("render.py" in line or line.startswith("systemctl stop ") for line in events)
    assert "systemctl enable --now bosun-boot-logo.path" in events
    assert "systemctl restart bosun-boot-logo.service" in events
