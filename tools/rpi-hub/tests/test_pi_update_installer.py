"""The root Pi self-update installer: signature first, safe extraction, rollback."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socketserver
import stat
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1]
SOURCE = TOOLS / "pi-update/install_package.py"
if os.name != "nt":  # fcntl and pwd are POSIX modules
    spec = importlib.util.spec_from_file_location("pi_update_installer", SOURCE)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)


def _ssh_keygen_signs():
    if os.name == "nt" or not shutil.which("ssh-keygen") or not shutil.which("bash"):
        return False
    result = subprocess.run(["ssh-keygen", "-Y", "sign"], capture_output=True, text=True)
    return "Too few arguments" in result.stderr or "usage" in result.stderr.lower()


pytestmark = pytest.mark.skipif(
    not _ssh_keygen_signs(), reason="the root installer needs POSIX directory descriptors, "
    "bash and ssh-keygen -Y; run these tests on Linux or WSL")

FAKE_TOOL = r'''#!/usr/bin/env bash
printf '%s %s\n' "${0##*/}" "$*" >> "$EVENT_LOG"
if [[ "${0##*/}" == systemctl && "$1" == is-active ]]; then
    if [[ -f "$FAIL_ACTIVE" ]] && grep -qx -- "${!#}" "$FAIL_ACTIVE"; then
        exit 3
    fi
fi
# A restart brings every unit back, as on a restored installation.
if [[ "${0##*/}" == systemctl && "$1" == restart ]]; then rm -f "$FAIL_ACTIVE"; fi
exit 0
'''

# Stands in for quick-install.sh/install.sh: replaces code, Stage, units and the
# installer itself, the way rsync --delete and install do.
QUICK_INSTALL = r'''#!/usr/bin/env bash
set -euo pipefail
tree="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
root="$BOSUN_TEST_ROOT"
echo "quick-install $(cat "$tree/release.txt")" >> "$EVENT_LOG"
if [[ "${SKIP_HUB:-0}" != 1 ]]; then
    rm -rf "$root/opt/bosun-hub/bosun_hub"
    cp -r "$tree/tools/rpi-hub/bosun_hub" "$root/opt/bosun-hub/bosun_hub"
fi
rm -f "$root/opt/bosun-hub/stage/old.js"
echo "new Stage" > "$root/opt/bosun-hub/stage/index.html"
echo "new hub unit" > "$root/etc/systemd/system/bosun-hub.service"
echo "added by the release" > "$root/etc/systemd/system/bosun-added.service"
ln -sf ../bosun-added.service "$root/etc/systemd/system/multi-user.target.wants/bosun-added.service"
rm -f "$root/etc/udev/rules.d/33-bosun-midi.rules"
echo "new installer" > "$root/usr/local/libexec/bosun-pi-update"
cp "$tree/tools/rpi-hub/allowed_signers" "$root/etc/bosun/allowed_signers"
echo "Bosun ready."
exit "${INSTALL_EXIT:-0}"
'''


class FakeHub(socketserver.ThreadingTCPServer):
    """Answers HUB_INFO with the release installed below the fixture root."""
    daemon_threads = allow_reuse_address = True

    def __init__(self, root):
        self.root = root
        super().__init__(("127.0.0.1", 0), HubHandler)


class HubHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        code = self.server.root / "opt/bosun-hub/bosun_hub/__init__.py"
        version = re.search(r'__version__ = "([^"]+)"', code.read_text()).group(1)
        # Unrelated Captain traffic may come first on the shared stream.
        self.wfile.write(b'{"type":"STATE","rig":1}\n')
        reply = {"type": "HUB_INFO", "id": request["id"], "version": version,
                 "pi_update": (self.server.root / "usr/local/libexec/bosun-pi-update").exists()}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


def keygen(path, comment="test"):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                   check=True, capture_output=True)
    return path.with_suffix(".pub").read_text().strip()


def tree_state(root):
    """Everything the installer may change, with contents, link targets and modes."""
    state, skipped = {}, ("var/lib/bosun-pi-update", "home", "var/lib/bosun-hub")
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names
                    if not Path(directory, name).relative_to(root).as_posix().startswith(skipped)]
        for name in names + files:
            path = Path(directory, name)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                value = "-> " + os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                value = "dir"
            else:
                value = hashlib.sha256(path.read_bytes()).hexdigest()
            state[path.relative_to(root).as_posix()] = (value, stat.S_IMODE(info.st_mode))
    return state


@pytest.fixture
def pi(tmp_path, monkeypatch):
    root = tmp_path / "pi"
    home = root / "home/bosun"
    jobs = home / ".local/state/bosun/updates"
    for directory in (jobs, root / "var/lib/bosun-hub/pi-update", root / "etc/bosun",
                      root / "opt/bosun-hub/bosun_hub", root / "opt/bosun-hub/stage",
                      root / "etc/systemd/system/multi-user.target.wants",
                      root / "etc/systemd/system/bosun-hub.service.d",
                      root / "etc/systemd/system/getty@tty1.service.d",
                      root / "etc/udev/rules.d", root / "usr/local/libexec"):
        directory.mkdir(parents=True, exist_ok=True)
    # The installed 0.8.5 release.
    (root / "opt/bosun-hub/bosun_hub/__init__.py").write_text('__version__ = "0.8.5"\n')
    (root / "opt/bosun-hub/stage/index.html").write_text("old Stage\n")
    (root / "opt/bosun-hub/stage/old.js").write_text("old bundle\n")
    (root / "opt/bosun-hub/kiosk.sh").write_text("#!/bin/sh\n")
    (root / "opt/bosun-hub/kiosk.sh").chmod(0o755)
    units = root / "etc/systemd/system"
    (units / "bosun-hub.service").write_text("old hub unit\n")
    (units / "bosun-hub.service.d/update-timeout.conf").write_text("[Service]\n")
    (units / "getty@tty1.service.d/50-bosun-splash.conf").write_text("[Unit]\n")
    (units / "multi-user.target.wants/bosun-hub.service").symlink_to("../bosun-hub.service")
    (units / "unrelated.service").write_text("not Bosun\n")
    (root / "etc/udev/rules.d/33-bosun-midi.rules").write_text("old rule\n")
    (root / "usr/local/libexec/bosun-pi-update").write_text("old installer\n")
    (root / "usr/local/libexec/bosun-pi-update").chmod(0o755)
    keys = tmp_path / "keys"
    keys.mkdir()
    public = keygen(keys / "release", "release")
    allowed = f'bosun-release namespaces="bosun-pi-package" {public}\n'
    (root / "etc/bosun/allowed_signers").write_text(allowed)
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    for name in ("systemctl", "udevadm"):
        (binary / name).write_text(FAKE_TOOL)
        (binary / name).chmod(0o755)
    events = tmp_path / "events.log"
    events.touch()
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BOSUN_TEST_ROOT", str(root))
    monkeypatch.setenv("EVENT_LOG", str(events))
    monkeypatch.setenv("FAIL_ACTIVE", str(tmp_path / "fail-active"))
    hub = FakeHub(root)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    state = SimpleNamespace(root=root, home=home, jobs=jobs, keys=keys, allowed=allowed,
                            events=events, hub=hub, tmp=tmp_path)
    state.ctx = SimpleNamespace(root=root, home=home, state=root / "var/lib/bosun-pi-update",
                                hub=f"127.0.0.1:{hub.server_address[1]}", health_timeout=3,
                                install_timeout=30)
    yield state
    hub.shutdown()
    hub.server_close()


def package(pi, version="0.8.6", extra=(), allowed=None):
    """A Bosun Pi package as tools/package-pi-setup.py writes it (plus extra members)."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        files = {"release.txt": version, "tools/rpi-hub/quick-install.sh": QUICK_INSTALL,
                 "tools/rpi-hub/bosun_hub/__init__.py": f'"""Hub."""\n\n__version__ = "{version}"\n',
                 "tools/rpi-hub/allowed_signers": allowed or pi.allowed,
                 "editor/dist-stage/index.html": "<p>Stage</p>\n"}
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return stream.getvalue()


def sign(pi, data, key="release", namespace="bosun-pi-package"):
    message = pi.keys / f"message-{secrets.token_hex(4)}"
    message.write_bytes(data)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(pi.keys / key), "-n", namespace, str(message)],
                   check=True, capture_output=True)
    return message.with_name(message.name + ".sig").read_text()


def stage(pi, data, signature=None, request=True):
    """What the hub stores for a committed pi job."""
    job = secrets.token_hex(16)
    directory = pi.jobs / job
    directory.mkdir(mode=0o700)
    (directory / "package.tar.gz").write_bytes(data)
    (directory / "package.sig").write_text(signature if signature is not None else sign(pi, data))
    (directory / "status.json").write_text(json.dumps({"job": job, "kind": "pi", "phase": "installing"}))
    if request:
        (pi.root / "var/lib/bosun-hub/pi-update/request").write_text(job + "\n")
    return job


def run(pi):
    results = installer.run(pi.ctx)
    for result in results or ():
        path = pi.ctx.state / f"{result['job']}.json"
        assert json.loads(path.read_text()) == result
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert sorted(result) == ["detail", "error", "job", "phase", "version"]
    return results


def events(pi):
    return pi.events.read_text().splitlines()


def test_signed_package_is_installed_and_checked(pi):
    job = stage(pi, package(pi))
    [result] = run(pi)
    assert result == {"job": job, "phase": "done", "version": "0.8.6", "error": None, "detail": "Installed"}
    assert (pi.root / "opt/bosun-hub/stage/index.html").read_text() == "new Stage\n"
    assert '"0.8.6"' in (pi.root / "opt/bosun-hub/bosun_hub/__init__.py").read_text()
    assert "quick-install 0.8.6" in events(pi)
    assert "systemctl is-active --quiet bosun-hub.service" in events(pi)
    # The private copy, the tree and the snapshot are gone; only the result stays.
    assert sorted(path.name for path in pi.ctx.state.iterdir()) == [f"{job}.json", "lock"]
    # The hub's files were only read.
    assert (pi.jobs / job / "package.tar.gz").exists()


def test_repeated_request_never_installs_a_job_twice(pi):
    job = stage(pi, package(pi))
    run(pi)
    before = events(pi)
    assert run(pi) == []
    assert events(pi) == before
    assert json.loads((pi.ctx.state / f"{job}.json").read_text())["phase"] == "done"


def refused(pi, job, message):
    """The job ended without a single installed file changing."""
    [result] = run(pi)
    assert result["job"] == job and result["phase"] == "error", result
    assert re.search(message, result["error"]), result["error"]
    assert result["detail"] == "Nothing was changed"
    assert not any(line.startswith("quick-install") for line in events(pi))
    assert not (pi.ctx.state / job).exists()
    return result


@pytest.mark.parametrize("case", ["other-key", "wrong-namespace", "wrong-identity", "tampered",
                                  "garbage-signature", "missing-signature"])
def test_package_without_a_valid_release_signature_is_refused_before_extraction(pi, monkeypatch, case):
    before = tree_state(pi.root)
    data = package(pi)
    signature = None
    if case == "other-key":
        keygen(pi.keys / "attacker")
        signature = sign(pi, data, key="attacker")
    elif case == "wrong-namespace":
        signature = sign(pi, data, namespace="file")
    elif case == "wrong-identity":
        public = (pi.keys / "release.pub").read_text().strip()
        (pi.root / "etc/bosun/allowed_signers").write_text(f'someone-else namespaces="bosun-pi-package" {public}\n')
        before = tree_state(pi.root)
    elif case == "tampered":
        signature = sign(pi, data)
        data = package(pi, version="6.6.6")
    elif case == "garbage-signature":
        signature = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"
    job = stage(pi, data, signature)
    if case == "missing-signature":
        (pi.jobs / job / "package.sig").unlink()
    extracted = []
    monkeypatch.setattr(installer, "extract", lambda *args: extracted.append(args))
    refused(pi, job, "not signed with the Bosun release key" if case != "missing-signature"
            else "not staged a complete")
    assert extracted == []
    assert tree_state(pi.root) == before


@pytest.mark.parametrize("anchor", ["missing", "group-writable", "symlink"])
def test_untrusted_or_missing_trust_anchor_is_refused(pi, anchor):
    path = pi.root / "etc/bosun/allowed_signers"
    if anchor == "missing":
        path.unlink()
    elif anchor == "group-writable":
        path.chmod(0o664)
    else:
        (pi.tmp / "elsewhere").write_text(pi.allowed)
        path.unlink()
        path.symlink_to(pi.tmp / "elsewhere")
    job = stage(pi, package(pi))
    refused(pi, job, "allowed_signers")


def member(name, kind=tarfile.REGTYPE, data=b"x", **fields):
    info = tarfile.TarInfo(name)
    info.type, info.size = kind, len(data) if kind == tarfile.REGTYPE else 0
    for key, value in fields.items():
        setattr(info, key, value)
    return info, data if kind == tarfile.REGTYPE else None


@pytest.mark.parametrize("hostile, message", [
    ([member("/etc/escape")], "unsafe path"),
    ([member("../escape")], "unsafe path"),
    ([member("tools/../../escape")], "unsafe path"),
    ([member("tools//escape")], "unsafe path"),
    ([member("tools/link", tarfile.SYMTYPE, linkname="/etc/shadow")], "not a regular file"),
    ([member("tools/hard", tarfile.LNKTYPE, linkname="release.txt")], "not a regular file"),
    ([member("tools/device", tarfile.CHRTYPE, devmajor=1, devminor=3)], "not a regular file"),
    ([member("tools/pipe", tarfile.FIFOTYPE)], "not a regular file"),
    ([member("release.txt")], "duplicate"),
    ([member(f"many/{index}") for index in range(20)], "too many files"),
    ([member("big.bin", data=b"\0" * 5000)], "size limit"),
], ids=["absolute", "parent", "nested-parent", "empty-part", "symlink", "hardlink", "device",
        "fifo", "duplicate", "count", "size"])
def test_hostile_members_are_refused_even_when_signed(pi, monkeypatch, hostile, message):
    monkeypatch.setattr(installer, "MAX_MEMBERS", 24)
    monkeypatch.setattr(installer, "MAX_EXTRACTED", 4096)
    before = tree_state(pi.root)
    job = stage(pi, package(pi, extra=hostile))
    refused(pi, job, message)
    assert tree_state(pi.root) == before
    assert not (pi.tmp / "escape").exists() and not (pi.root / "escape").exists()


def test_decompression_beyond_the_cap_is_refused(pi, monkeypatch):
    monkeypatch.setattr(installer, "MAX_MEMBERS", 4)
    monkeypatch.setattr(installer, "MAX_EXTRACTED", 1024)
    # A 1 MB extended header compresses to a few KB and declares no file size.
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info, data = member("release.txt", data=b"0.8.6")
        info.pax_headers = {"comment": "x" * 1_000_000}
        tar.addfile(info, io.BytesIO(data))
    assert len(stream.getvalue()) < 64 * 1024
    job = stage(pi, stream.getvalue())
    refused(pi, job, "expands beyond its size limit")


def test_capped_stream_stops_at_its_limit():
    stream = installer.Capped(io.BytesIO(b"x" * 100), 60)
    assert stream.read(50) == b"x" * 50 and stream.read(10) == b"x" * 10
    with pytest.raises(ValueError, match="size limit"):
        stream.read()


def test_package_without_a_hub_version_is_not_a_bosun_package(pi):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        info, data = member("tools/rpi-hub/quick-install.sh", data=QUICK_INSTALL.encode())
        tar.addfile(info, io.BytesIO(data))
    job = stage(pi, stream.getvalue())
    refused(pi, job, "not a Bosun Pi package")


@pytest.mark.parametrize("staged", ["symlinked-package", "symlinked-job", "fifo-signature", "oversize"])
def test_hub_files_are_read_without_following_links(pi, staged):
    data = package(pi)
    job = stage(pi, data)
    directory = pi.jobs / job
    if staged == "symlinked-package":
        (pi.tmp / "real.tar.gz").write_bytes(data)
        (directory / "package.tar.gz").unlink()
        (directory / "package.tar.gz").symlink_to(pi.tmp / "real.tar.gz")
    elif staged == "symlinked-job":
        shutil.move(directory, pi.tmp / "real-job")
        directory.symlink_to(pi.tmp / "real-job")
    elif staged == "fifo-signature":
        (directory / "package.sig").unlink()
        os.mkfifo(directory / "package.sig")
    else:
        with (directory / "package.tar.gz").open("ab") as out:
            out.write(b"\0" * (4 * 1024 * 1024))
    [result] = run(pi)
    assert result["phase"] == "error" and result["detail"] == "Nothing was changed"
    assert not any(line.startswith("quick-install") for line in events(pi))


def test_failed_install_restores_the_previous_release(pi, monkeypatch):
    before = tree_state(pi.root)
    monkeypatch.setenv("INSTALL_EXIT", "1")
    job = stage(pi, package(pi))
    [result] = run(pi)
    assert result["phase"] == "rolled-back" and result["version"] == "0.8.6"
    assert result["error"] == "quick-install.sh failed with exit status 1: Bosun ready."
    assert result["detail"] == "The previous installation was restored"
    assert tree_state(pi.root) == before  # added units gone, links and modes back
    assert (pi.root / "etc/systemd/system/multi-user.target.wants/bosun-hub.service").is_symlink()
    assert "systemctl restart bosun-hub.service bosun-kiosk.service bosun-midi.timer" in events(pi)
    assert "udevadm control --reload" in events(pi)
    assert not (pi.ctx.state / job).exists()
    assert not list(pi.root.rglob(".bosun-*"))


def test_unhealthy_release_is_rolled_back(pi, monkeypatch):
    before = tree_state(pi.root)
    monkeypatch.setenv("SKIP_HUB", "1")  # the hub keeps answering with 0.8.5
    stage(pi, package(pi))
    [result] = run(pi)
    assert result["phase"] == "rolled-back"
    assert "the hub reports version 0.8.5, not 0.8.6" in result["error"]
    assert tree_state(pi.root) == before


def test_inactive_service_after_install_is_rolled_back(pi):
    (pi.tmp / "fail-active").write_text("bosun-kiosk.service\n")
    before = tree_state(pi.root)
    stage(pi, package(pi))
    [result] = run(pi)
    assert result["phase"] == "rolled-back" and "bosun-kiosk.service is not active" in result["error"]
    assert tree_state(pi.root) == before


def test_failed_restore_keeps_the_snapshot_and_reports_manual_recovery(pi, monkeypatch):
    monkeypatch.setenv("INSTALL_EXIT", "1")
    job = stage(pi, package(pi))
    monkeypatch.setattr(installer, "restart", lambda ctx: (_ for _ in ()).throw(OSError("systemd is gone")))
    [result] = run(pi)
    assert result["phase"] == "error" and result["detail"] == "Manual recovery required"
    assert "systemd is gone" in result["error"] and "exit status 1" in result["error"]
    assert (pi.ctx.state / job / "snapshot/manifest.json").is_file()


def test_refuses_while_a_captain_update_runs(pi):
    before = tree_state(pi.root)
    captain = pi.jobs / ("b" * 32)
    captain.mkdir()
    (captain / "status.json").write_text(json.dumps({"job": "b" * 32, "phase": "flashing"}))
    job = stage(pi, package(pi))
    refused(pi, job, "Captain firmware update is running")
    assert tree_state(pi.root) == before


@pytest.mark.parametrize("phase", ["recovery-required", "done", "uploading"])
def test_captain_jobs_that_do_not_run_allow_the_update(pi, phase):
    captain = pi.jobs / ("b" * 32)
    captain.mkdir()
    (captain / "status.json").write_text(json.dumps({"job": "b" * 32, "phase": phase}))
    stage(pi, package(pi))
    assert run(pi)[0]["phase"] == "done"


def test_interrupted_install_is_rolled_back_on_the_next_request(pi):
    before = tree_state(pi.root)
    job = stage(pi, package(pi))
    work = pi.ctx.state / job
    pi.ctx.state.mkdir(parents=True)
    work.mkdir(mode=0o700)
    installer.snapshot(pi.root, work)
    (pi.ctx.state / f"{job}.json").write_text(json.dumps(
        {"job": job, "phase": "installing", "version": "0.8.6", "error": None, "detail": "Installing"}))
    # The power went out halfway through install.sh.
    (pi.root / "opt/bosun-hub/stage/index.html").write_text("half-installed\n")
    (pi.root / "etc/systemd/system/bosun-added.service").write_text("new\n")
    [result] = run(pi)
    assert result["phase"] == "rolled-back" and "interrupted" in result["error"]
    assert tree_state(pi.root) == before
    assert not any(line.startswith("quick-install") for line in events(pi))


def test_startup_run_does_not_install_an_expired_request(pi):
    job = stage(pi, package(pi))
    request = pi.root / "var/lib/bosun-hub/pi-update/request"
    os.utime(request, (request.stat().st_atime - 600, request.stat().st_mtime - 600))
    before = tree_state(pi.root)
    [result] = run(pi)
    assert result["job"] == job and result["phase"] == "error" and "expired" in result["error"]
    assert tree_state(pi.root) == before and not any(line.startswith("quick-install") for line in events(pi))
    assert run(pi) == []  # and it stays finished


def test_startup_run_without_a_request_does_nothing(pi):
    assert run(pi) == []
    assert events(pi) == []


def test_interrupted_before_the_snapshot_changed_nothing(pi):
    job = stage(pi, package(pi))
    pi.ctx.state.mkdir(parents=True)
    (pi.ctx.state / f"{job}.json").write_text(json.dumps(
        {"job": job, "phase": "installing", "version": None, "error": None, "detail": "Checking the package"}))
    [result] = run(pi)
    assert result["phase"] == "error" and "before anything was changed" in result["error"]


def test_catches_up_with_a_request_written_during_the_run(pi, monkeypatch):
    first = stage(pi, package(pi))
    second_data = package(pi, version="0.8.7")
    original, staged = installer.install, []

    def install_then_request(ctx, status):
        result = original(ctx, status)
        if not staged:
            staged.append(stage(pi, second_data))  # its path event is lost
        return result

    monkeypatch.setattr(installer, "install", install_then_request)
    results = run(pi)
    assert [(result["job"], result["phase"], result["version"]) for result in results] == [
        (first, "done", "0.8.6"), (staged[0], "done", "0.8.7")]


def test_a_second_run_waits_for_the_lock_holder(pi):
    import fcntl
    stage(pi, package(pi))
    pi.ctx.state.mkdir(parents=True)
    lock = os.open(pi.ctx.state / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert installer.run(pi.ctx) is None
    finally:
        os.close(lock)
    assert not any(line.startswith("quick-install") for line in events(pi))


@pytest.mark.parametrize("content", ["", "../../etc\n", "A" * 32 + "\n"])
def test_invalid_request_is_ignored(pi, content):
    stage(pi, package(pi), request=False)
    (pi.root / "var/lib/bosun-hub/pi-update/request").write_text(content)
    assert run(pi) == []


def test_unsafe_state_directory_is_refused(pi):
    pi.ctx.state.mkdir(parents=True)
    pi.ctx.state.chmod(0o777)
    stage(pi, package(pi))
    with pytest.raises(ValueError, match="root-owned"):
        installer.run(pi.ctx)


def test_snapshot_covers_what_install_replaces_and_nothing_else(pi):
    assert installer.installed(pi.root) == [
        "etc/bosun/allowed_signers", "etc/systemd/system/bosun-hub.service",
        "etc/systemd/system/bosun-hub.service.d",
        "etc/systemd/system/getty@tty1.service.d/50-bosun-splash.conf",
        "etc/systemd/system/multi-user.target.wants/bosun-hub.service",
        "etc/udev/rules.d/33-bosun-midi.rules", "opt/bosun-hub", "usr/local/libexec/bosun-pi-update"]


def test_command_line_reports_the_result(pi, monkeypatch):
    command = [sys.executable, "-I", str(SOURCE), "--root", str(pi.root), "--home", str(pi.home),
               "--hub", pi.ctx.hub, "--health-timeout", "3"]
    job = stage(pi, package(pi))
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Bosun Pi update {job}: done, Installed" in result.stdout
    assert "Good \"bosun-pi-package\" signature" in result.stdout
    monkeypatch.setenv("INSTALL_EXIT", "1")
    job = stage(pi, package(pi, version="0.8.7"))
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and f"{job}: rolled-back: quick-install.sh failed" in result.stderr


def test_hub_and_installer_agree_on_the_job(pi):
    sys.path.insert(0, str(TOOLS))
    from bosun_hub.update_service import UpdateService

    replies = []
    hub = SimpleNamespace(_offer_correlated=lambda sub, has_id, request_id, reply: replies.append(reply),
                          link=SimpleNamespace(port_name=None, connected=False))
    files = [pi.root / path for path in ("usr/local/libexec/bosun-pi-update", "etc/bosun/allowed_signers")]
    service = UpdateService(hub, root=pi.jobs, pi_request_dir=pi.root / "var/lib/bosun-hub/pi-update",
                            pi_status_dir=pi.ctx.state, pi_installer=files)

    def send(message_type, **args):
        assert service.handle(None, {"type": message_type, **args})
        return replies.pop()

    data = package(pi)
    job = send("HUB_UPDATE_BEGIN", kind="pi", size=len(data), sha256=hashlib.sha256(data).hexdigest(),
               signature=sign(pi, data))["job"]
    for offset in range(0, len(data), 16384):
        send("HUB_UPDATE_CHUNK", job=job, offset=offset, data=base64.b64encode(data[offset:offset + 16384]).decode())
    assert send("HUB_UPDATE_COMMIT", job=job)["phase"] == "installing"
    [result] = run(pi)
    assert result["phase"] == "done"
    state = send("HUB_UPDATE_STATUS", job=job)
    assert (state["phase"], state["version"], state["error"]) == ("done", "0.8.6", "")
