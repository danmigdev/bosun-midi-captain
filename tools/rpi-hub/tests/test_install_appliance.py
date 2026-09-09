"""Run the real appliance installers offline with isolated files and fake OS tools."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

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
cc)
    [[ "${FAIL_CONVERTER:-0}" != 1 ]] || exit 42
    output=''
    previous=''
    for argument in "$@"; do
        [[ "$previous" != -o ]] || output="$argument"
        if [[ "$argument" == *.c && ! -f "$argument" ]]; then exit 43; fi
        previous="$argument"
    done
    safe_destination "$output"
    printf 'offline converter artifact\n' > "$output"
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
        for prefix in ("/opt/bosun-hub", "/etc/systemd/system", "/etc/udev/rules.d", "/var/lib/bosun-hub"):
            script = script.replace(prefix, shell_path(system) + prefix)
        (source / filename).write_text(script, encoding="utf-8", newline="\n")
    shutil.copytree(SOURCE / "systemd", source / "systemd")
    shutil.copytree(SOURCE / "udev", source / "udev")
    shutil.copytree(SOURCE / "kiosk", source / "kiosk")
    (source / "bosun_hub").mkdir()
    (source / "bosun_hub/__main__.py").write_text("# installed module fixture\n")
    (source / "requirements.txt").write_text("# offline fixture\n")
    (source / "README.md").write_text("Offline installer fixture.\n")
    native = checkout / "firmware-native"
    for filename in (
        "platform/host/storage_image.c", "platform/rp2040/storage.c",
        "src/storage_path.c", "src/config.c", "src/json.c",
        "third_party/littlefs/lfs.c", "third_party/littlefs/lfs_util.c",
        "third_party/littlefs/lfs.h", "third_party/littlefs/lfs_util.h",
        "include/bosun/board.h", "include/bosun/config.h",
        "include/bosun/json.h", "include/bosun/storage.h",
    ):
        target = native / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("/* compiler input fixture */\n")
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    for name in (
        "apt-get", "id", "useradd", "getent", "usermod", "rsync", "install",
        "chown", "cc", "picotool", "udevadm", "systemctl", "stat", "runuser",
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
    instance.native = native
    return instance


@pytest.mark.parametrize("missing", [
    "stage", "platform/host/storage_image.c", "src/config.c",
    "include/bosun/config.h", "third_party/littlefs/lfs.c",
])
def test_missing_build_or_checkout_fails_before_any_appliance_mutation(appliance, missing):
    if missing != "stage":
        appliance.build_stage()
        (appliance.native / missing).unlink()
    result, events = appliance.run()
    assert result.returncode != 0
    assert events == []


@pytest.mark.parametrize("entry", ["stage-kiosk.html", "index.html"])
def test_fresh_install_sets_access_and_native_support_before_starting_services(appliance, entry):
    appliance.build_stage(entry)
    result, events = appliance.run(EXISTING_USER="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (appliance.installed_stage / "index.html").read_text() == "<p>new Stage</p>\n"
    assert (appliance.system / "opt/bosun-hub/bin/bosun_storage_image").is_file()
    assert (appliance.system / "etc/udev/rules.d/60-bosun-update.rules").is_file()
    assert (appliance.system / "etc/udev/rules.d/99-bosun-kiosk-input.rules").is_file()
    first_restart = next(i for i, line in enumerate(events) if line.startswith("systemctl restart "))
    before_start = events[:first_restart]
    assert before_start.index("udevadm control --reload") < before_start.index(
        "udevadm trigger --action=change --subsystem-match=input"
    ) < before_start.index("udevadm settle --timeout=10")
    assert any(line.startswith("apt-get install ") and "build-essential" in line
               and "picotool" in line and "seatd" in line for line in before_start)
    assert any(line.startswith("cc ") for line in before_start)
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


def test_converter_build_failure_does_not_restart_running_services(appliance):
    appliance.build_stage()
    result, events = appliance.run(FAIL_CONVERTER="1")
    assert result.returncode == 42
    assert not any(line.startswith("systemctl restart ") for line in events)
    assert not any(line.startswith("systemctl enable --now ") for line in events)


@pytest.mark.parametrize("failed_service", ["bosun-hub.service", "bosun-kiosk.service"])
def test_inactive_service_is_reported_as_failed_install(appliance, failed_service):
    appliance.build_stage()
    result, _ = appliance.run(FAIL_SERVICE=failed_service)
    assert result.returncode != 0
