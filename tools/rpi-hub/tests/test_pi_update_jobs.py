"""The hub side of the Pi self-update: HUB_INFO, kind "pi" jobs and root's status."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bosun_hub import __version__
from bosun_hub.hub import Hub
from bosun_hub.update_service import UpdateService

SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQ==\n-----END SSH SIGNATURE-----\n"
PACKAGE = b"signed Pi package bytes"


class Link:
    connected = True
    port_name = "/dev/ttyACM1"
    usb_identity = ("/sys/fake/captain", "DF635C76C783412E", "02")
    _thread = None

    def __init__(self):
        self.sent = []
        self.stops = 0

    def send(self, line):
        self.sent.append(line)
        return True

    def stop(self):
        self.stops += 1
        self.connected = False


class Installer:
    def __init__(self):
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        return {"status": "complete"}


def make_hub(tmp_path, *, installed=True, captain=True):
    hub = Hub(None)
    hub.link = Link()
    hub._update_device_info = {"type": "DEVICE_INFO", "fw": "0.8.5", "device": "midi_captain"}
    files = [tmp_path / "root" / name for name in (
        "bosun-pi-update.path", "bosun-pi-update.service", "bosun-pi-update", "allowed_signers")]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        if installed:
            path.write_text("installed\n")
    request_dir = tmp_path / "hub-state/pi-update"
    status_dir = tmp_path / "pi-update-state"
    request_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(exist_ok=True)
    hub.updates = UpdateService(hub, root=tmp_path / "updates", installer=Installer(),
                                pi_request_dir=request_dir, pi_status_dir=status_dir, pi_installer=files)
    hub.updates.supported = lambda: captain
    hub._restart_update_link = lambda: setattr(hub.link, "connected", True)
    return hub, hub.subscribe()


def request(sub, message_type, **args):
    sub.send(json.dumps({"type": message_type, "id": "local-1", **args}))
    response = json.loads(sub._queue.get_nowait())
    assert response["id"] == "local-1"
    return response


def begin(sub, data=PACKAGE, **overrides):
    args = {"kind": "pi", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "signature": SIGNATURE, **overrides}
    return request(sub, "HUB_UPDATE_BEGIN", **args)


def upload(sub, data=PACKAGE):
    state = begin(sub, data)
    assert state["phase"] == "uploading" and state["kind"] == "pi", state
    result = request(sub, "HUB_UPDATE_CHUNK", job=state["job"], offset=0,
                     data=base64.b64encode(data).decode())
    assert result["received"] == len(data)
    return state["job"]


def root_status(hub, job, phase, **fields):
    status = {"job": job, "phase": phase, "version": "0.8.6", "error": None,
              "detail": "Installed", **fields}
    (hub.updates.pi_status_dir / f"{job}.json").write_text(json.dumps(status))


def captain_upload(sub, data=b"archive"):
    state = request(sub, "HUB_UPDATE_BEGIN", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert state["phase"] == "uploading" and "kind" not in state
    request(sub, "HUB_UPDATE_CHUNK", job=state["job"], offset=0, data=base64.b64encode(data).decode())
    return state["job"]


@pytest.mark.parametrize("installed", [True, False])
def test_hub_info_is_answered_by_the_hub(tmp_path, installed):
    hub, sub = make_hub(tmp_path, installed=installed)
    assert request(sub, "HUB_INFO") == {"type": "HUB_INFO", "id": "local-1", "version": __version__,
                                        "pi_update": installed}
    hub.updates.maintenance = True  # a Captain update does not hide the hub release
    assert request(sub, "HUB_INFO")["version"] == __version__
    assert hub.link.sent == []


def test_hub_info_needs_every_installer_file_and_a_writable_request(tmp_path):
    hub, sub = make_hub(tmp_path)
    hub.updates.pi_installer[3].unlink()  # no trust anchor
    assert request(sub, "HUB_INFO")["pi_update"] is False
    hub, sub = make_hub(tmp_path / "other")
    hub.updates.pi_request_dir.rmdir()
    assert request(sub, "HUB_INFO")["pi_update"] is False


def test_version_is_a_release_number():
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pi_job_stages_package_and_signature_and_commit_requests_root(tmp_path):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    directory = hub.updates.root / job
    assert (directory / "package.tar.gz").read_bytes() == PACKAGE
    assert (directory / "package.sig").read_bytes() == SIGNATURE.encode()
    state = request(sub, "HUB_UPDATE_COMMIT", job=job)
    assert state["phase"] == "installing" and state["kind"] == "pi"
    assert (hub.updates.pi_request_dir / "request").read_bytes() == job.encode() + b"\n"
    # A lost COMMIT reply is answered again without a second request.
    (hub.updates.pi_request_dir / "request").unlink()
    assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "installing"
    assert not (hub.updates.pi_request_dir / "request").exists()
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "installing"
    assert request(sub, "HUB_UPDATE_ABORT", job=job)["type"] == "ERROR"
    # The Captain is never involved.
    assert hub.link.stops == 0 and hub.link.sent == [] and hub.updates.installer.calls == []
    assert not hub.updates.maintenance and hub.updates.task is None


@pytest.mark.parametrize("phase", ["done", "rolled-back", "error"])
def test_status_follows_root_and_keeps_its_result(tmp_path, phase):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    request(sub, "HUB_UPDATE_COMMIT", job=job)
    root_status(hub, job, "installing", detail="Installing")
    state = request(sub, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == "installing" and state["detail"] == "Installing"
    error = None if phase == "done" else "quick-install.sh failed with exit status 1"
    root_status(hub, job, phase, error=error)
    state = request(sub, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == phase and state["version"] == "0.8.6"
    assert state["error"] == (error or "")
    (hub.updates.pi_status_dir / f"{job}.json").unlink()
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == phase
    # A finished job never blocks the next update.
    assert begin(sub)["phase"] == "uploading"


@pytest.mark.parametrize("status", ['{"job": "other", "phase": "done"}', '{"phase": "done"}',
                                    "not JSON", '{"job": "JOB", "phase": "finished"}'])
def test_unrelated_or_unreadable_root_status_is_not_a_result(tmp_path, status):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    request(sub, "HUB_UPDATE_COMMIT", job=job)
    (hub.updates.pi_status_dir / f"{job}.json").write_text(status.replace("JOB", job))
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "installing"


def test_a_root_installer_that_never_starts_ends_the_job(tmp_path):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    request(sub, "HUB_UPDATE_COMMIT", job=job)
    state = hub.updates._load(job)
    state["committed"] = time.time() - 301
    hub.updates._save(state)
    state = request(sub, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == "error" and "did not start" in state["error"]


def unplugged(hub):
    hub.link.connected = False
    hub._update_device_info = None
    hub.link.port_name = hub.link.usb_identity = None


def test_a_pi_job_does_not_need_the_captain(tmp_path):
    hub, sub = make_hub(tmp_path, captain=False)
    unplugged(hub)
    assert request(sub, "PING")["error"] == "link_down"
    job = upload(sub)
    # Desktop follows the job with PING: the hub answers while it is unfinished.
    assert request(sub, "PING") == {"type": "ACK", "id": "local-1"}
    assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "installing"
    assert request(sub, "PING")["type"] == "ACK"
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "installing"
    assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    assert request(sub, "GET_DEVICE_INFO")["error"] == "link_down"  # only PING is answered
    root_status(hub, job, "done")
    # The result ends the local answer even before Desktop asks for it.
    assert request(sub, "PING")["error"] == "link_down"
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "done"
    assert hub.link.sent == []


def test_restarted_hub_answers_ping_and_status_without_the_captain(tmp_path):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    request(sub, "HUB_UPDATE_COMMIT", job=job)
    root_status(hub, job, "installing", detail="Installing")
    restarted, client = make_hub(tmp_path)
    unplugged(restarted)
    assert request(client, "PING")["type"] == "ACK"  # the reloaded job, before any STATUS
    state = request(client, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == "installing" and state["detail"] == "Installing"
    root_status(restarted, job, "rolled-back", error="Health check failed")
    assert request(client, "PING")["error"] == "link_down"
    assert request(client, "HUB_UPDATE_STATUS", job=job)["phase"] == "rolled-back"
    assert restarted.link.sent == []


def test_abandoned_or_aborted_pi_upload_stops_answering_ping(tmp_path):
    hub, sub = make_hub(tmp_path)
    unplugged(hub)
    job = upload(sub)
    assert request(sub, "PING")["type"] == "ACK"
    request(sub, "HUB_UPDATE_ABORT", job=job)
    assert request(sub, "PING")["error"] == "link_down"
    job = upload(sub)
    state = hub.updates._load(job)
    state["created"] = time.time() - 901
    hub.updates._save(state)
    restarted, client = make_hub(tmp_path)
    unplugged(restarted)
    assert request(client, "PING")["error"] == "link_down"


def test_restarted_hub_keeps_answering_and_asks_root_again(tmp_path):
    hub, sub = make_hub(tmp_path)
    job = upload(sub)
    request(sub, "HUB_UPDATE_COMMIT", job=job)
    root_status(hub, job, "installing", detail="Installing")
    request_file = hub.updates.pi_request_dir / "request"
    request_file.unlink()
    restarted, client = make_hub(tmp_path)
    restarted.updates.start()
    assert request_file.read_bytes() == job.encode() + b"\n"
    state = request(client, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == "installing" and state["kind"] == "pi"
    assert restarted.updates.allows_local_ping()
    root_status(restarted, job, "rolled-back", error="Health check failed")
    state = request(client, "HUB_UPDATE_STATUS", job=job)
    assert state["phase"] == "rolled-back" and state["error"] == "Health check failed"
    request_file.unlink()
    make_hub(tmp_path)[0].updates.start()  # a finished job is not requested again
    assert not request_file.exists()


def test_start_ignores_captain_jobs_and_unsupported_hubs(tmp_path):
    hub, sub = make_hub(tmp_path)
    captain_upload(sub)
    hub.updates.start()
    assert not (hub.updates.pi_request_dir / "request").exists()
    job = upload(make_hub(tmp_path / "pi")[1])
    hub, _ = make_hub(tmp_path / "pi")
    state = hub.updates._load(job)
    state.update(phase="installing", committed=time.time())
    hub.updates._save(state)
    for path in hub.updates.pi_installer:
        path.unlink()
    hub.updates.start()
    assert not (hub.updates.pi_request_dir / "request").exists()


def test_tampered_or_incomplete_package_never_reaches_root(tmp_path):
    hub, sub = make_hub(tmp_path)
    state = begin(sub, PACKAGE)
    assert request(sub, "HUB_UPDATE_COMMIT", job=state["job"])["type"] == "ERROR"  # incomplete
    request(sub, "HUB_UPDATE_ABORT", job=state["job"])
    job = upload(sub)
    (hub.updates.root / job / "package.tar.gz").write_bytes(b"changed package bytes!")
    state = request(sub, "HUB_UPDATE_COMMIT", job=job)
    assert state["phase"] == "error" and "SHA-256" in state["error"]
    assert not (hub.updates.pi_request_dir / "request").exists()


@pytest.mark.parametrize("overrides, message", [
    ({"kind": "firmware"}, "Unknown update kind"),
    ({"signature": None}, "signature"),
    ({"signature": "ssh-ed25519 AAAA"}, "signature"),
    ({"signature": SIGNATURE.replace("U1NI", "U1NI" + "A" * 4096)}, "signature"),
    ({"signature": SIGNATURE.replace("U1NI", "é")}, "signature"),
    ({"size": 4 * 1024 * 1024 + 1}, "size limit"),
    ({"sha256": "0" * 63}, "SHA-256"),
])
def test_invalid_pi_begin_is_refused(tmp_path, overrides, message):
    hub, sub = make_hub(tmp_path)
    response = begin(sub, **overrides)
    assert response["type"] == "ERROR" and message in response["error"]
    assert not (tmp_path / "updates").exists() or not list((tmp_path / "updates").iterdir())


def test_hub_without_the_root_installer_refuses_pi_jobs(tmp_path):
    hub, sub = make_hub(tmp_path, installed=False)
    response = begin(sub)
    assert response["type"] == "ERROR" and "Setup guide" in response["error"]


def test_captain_jobs_and_pi_jobs_exclude_each_other(tmp_path):
    hub, sub = make_hub(tmp_path)
    captain = captain_upload(sub)
    assert "in progress" in begin(sub)["error"]  # a Captain job owns the slot while uploading
    state = hub.updates._load(captain)
    state["phase"] = "flashing"
    hub.updates._save(state)
    assert "in progress" in begin(sub)["error"]  # and while it runs
    hub.updates.active = None
    state["phase"] = "done"
    hub.updates._save(state)
    job = upload(sub)
    assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "installing"
    # While root installs, no Captain job can start.
    response = request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)
    assert response["type"] == "ERROR" and "in progress" in response["error"]
    assert "in progress" in begin(sub)["error"]


def test_captain_upload_cannot_commit_while_the_pi_installs(tmp_path):
    hub, sub = make_hub(tmp_path)
    captain = captain_upload(sub)
    other = "c" * 32
    (hub.updates.root / other).mkdir()
    hub.updates._save({"job": other, "kind": "pi", "phase": "installing", "size": 1, "sha256": "1" * 64,
                       "received": 1, "created": time.time(), "committed": time.time()})
    response = request(sub, "HUB_UPDATE_COMMIT", job=captain)
    assert response["type"] == "ERROR" and "Pi update" in response["error"]
    assert hub.link.stops == 0


def test_captain_recovery_evidence_does_not_block_a_pi_update(tmp_path):
    hub, sub = make_hub(tmp_path)
    captain = captain_upload(sub)
    state = hub.updates._load(captain)
    state["phase"] = "recovery-required"
    hub.updates._save(state)
    hub.updates.active = None
    assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    job = upload(sub)
    assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "installing"
    assert request(sub, "HUB_UPDATE_STATUS", job=captain)["phase"] == "recovery-required"


def test_pi_upload_survives_a_hub_restart_and_can_be_resumed(tmp_path):
    hub, sub = make_hub(tmp_path)
    state = begin(sub)
    request(sub, "HUB_UPDATE_CHUNK", job=state["job"], offset=0, data=base64.b64encode(PACKAGE[:5]).decode())
    restarted, client = make_hub(tmp_path)
    assert request(client, "HUB_UPDATE_STATUS", job=state["job"])["received"] == 5
    request(client, "HUB_UPDATE_CHUNK", job=state["job"], offset=5, data=base64.b64encode(PACKAGE[5:]).decode())
    assert request(client, "HUB_UPDATE_COMMIT", job=state["job"])["phase"] == "installing"


def test_crash_before_the_first_pi_status_is_an_aborted_upload(tmp_path):
    hub, _ = make_hub(tmp_path)
    orphan = hub.updates.root / ("d" * 32)
    orphan.mkdir(parents=True)
    (orphan / "package.tar.gz").write_bytes(b"")
    (orphan / "package.sig").write_text(SIGNATURE)
    assert hub.updates._load(orphan.name)["phase"] == "aborted"
