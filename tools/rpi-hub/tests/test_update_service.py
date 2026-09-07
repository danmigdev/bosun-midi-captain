"""Exercise real hub admission/correlation and durable update job boundaries."""
import asyncio
import base64
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bosun_hub.hub import Hub
from bosun_hub.update_service import UpdateService


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
    def __init__(self, result="complete"):
        self.calls = []
        self.keyword_calls = []
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
    def run(self, *args, **kwargs):
        self.calls.append(args)
        self.keyword_calls.append(kwargs)
        args[-1]({"phase": "flashing"})
        self.started.set()
        self.release.wait(3)
        return {"status": self.result, "backup_path": "/saved/full-flash.bin"}


def setup(root, installer=None):
    hub = Hub(None)
    hub.link = Link()
    hub._update_device_info = {"type": "DEVICE_INFO", "fw": "0.6.4", "device": "midi_captain"}
    hub.updates = UpdateService(hub, root=root, installer=installer or Installer())
    hub.updates.supported = lambda: True
    hub._restart_update_link = lambda: setattr(hub.link, "connected", True)
    return hub, hub.subscribe()


def request(sub, kind, **args):
    sub.send(json.dumps({"type": kind, "id": "local-1", **args}))
    response = json.loads(sub._queue.get_nowait())
    assert response["id"] == "local-1"
    return response


def upload(sub, data=b"archive"):
    state = request(sub, "HUB_UPDATE_BEGIN", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert state["phase"] == "uploading"
    result = request(sub, "HUB_UPDATE_CHUNK", job=state["job"], offset=0, data=base64.b64encode(data).decode())
    assert result["received"] == len(data)
    return state["job"]


def test_upload_retries_are_idempotent_and_private(tmp_path):
    hub, sub = setup(tmp_path)
    other = hub.subscribe()
    job = upload(sub)
    assert request(sub, "HUB_UPDATE_CHUNK", job=job, offset=0, data=base64.b64encode(b"archive").decode())["received"] == 7
    assert request(sub, "HUB_UPDATE_CHUNK", job=job, offset=0, data=base64.b64encode(b"changed").decode())["type"] == "ERROR"
    assert request(sub, "HUB_UPDATE_CHUNK", job=job, offset=8, data="YQ==")["type"] == "ERROR"
    assert request(sub, "HUB_UPDATE_CHUNK", job=job, offset=True, data="YQ==")["type"] == "ERROR"
    assert request(sub, "HUB_UPDATE_STATUS", job="../../escape")["type"] == "ERROR"
    assert other._queue.empty()
    assert hub.link.sent == []
    assert (tmp_path / job / "package.zip").read_bytes() == b"archive"


def test_incomplete_upload_and_parallel_owner_cannot_commit(tmp_path):
    hub, sub = setup(tmp_path)
    state = request(sub, "HUB_UPDATE_BEGIN", size=10, sha256="0" * 64)
    assert request(sub, "HUB_UPDATE_COMMIT", job=state["job"])["type"] == "ERROR"
    assert request(hub.subscribe(), "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    assert hub.link.stops == 0
    assert request(sub, "HUB_UPDATE_ABORT", job=state["job"])["phase"] == "aborted"
    assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["phase"] == "uploading"


def test_uploaded_job_survives_editor_and_service_restart(tmp_path):
    hub, sub = setup(tmp_path)
    job = upload(sub)
    sub.close()
    restarted, client = setup(tmp_path)
    state = request(client, "HUB_UPDATE_STATUS", job=job)
    assert state["received"] == state["size"] == 7
    assert state["sha256"] == hashlib.sha256(b"archive").hexdigest()
    assert request(client, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    assert request(client, "HUB_UPDATE_ABORT", job=job)["phase"] == "aborted"


def test_interrupted_flash_requires_recovery_and_preserves_job(tmp_path):
    hub, sub = setup(tmp_path)
    job = upload(sub)
    state = hub.updates._load(job)
    state.update(phase="flashing", backup="saved-image.bin")
    hub.updates._save(state)
    restarted, client = setup(tmp_path)
    status = request(client, "HUB_UPDATE_STATUS", job=job)
    assert status["phase"] == "recovery-required"
    assert status["backup"] == "saved-image.bin"
    assert request(client, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    assert (tmp_path / job / "package.zip").exists()


def test_maintenance_keeps_network_alive_without_forwarding_mutations(tmp_path):
    hub, sub = setup(tmp_path)
    hub.updates.maintenance = True
    assert request(sub, "PING")["type"] == "ACK"
    assert request(sub, "PUT_PATCH", patch={})["error"] == "maintenance_busy"
    assert request(sub, "GET_DEVICE_INFO")["error"] == "maintenance_busy"
    sub.send("not JSON")
    sub.send("[]")
    assert hub.link.sent == []
    assert request(sub, "HUB_UPDATE_INFO")["supported"] is True


def test_bad_archive_or_hash_never_takes_the_device(tmp_path):
    async def body():
        for tamper in (False, True):
            hub, sub = setup(tmp_path / str(tamper))
            job = upload(sub)
            if tamper:
                (hub.updates.root / job / "package.zip").write_bytes(b"changed")
            assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "validating"
            await hub.updates.wait()
            assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "error"
            assert hub.link.stops == 0
            assert hub.updates.installer.calls == []
    asyncio.run(body())


def test_commit_is_single_flight_and_survives_client_disconnect(tmp_path):
    async def body():
        installer = Installer()
        installer.release.clear()
        hub, sub = setup(tmp_path, installer)
        job = upload(sub)
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
            assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "validating"
            assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "validating"
            sub.close()
            assert await asyncio.to_thread(installer.started.wait, 2)
            other = hub.subscribe()
            assert request(other, "HUB_UPDATE_ABORT", job=job)["type"] == "ERROR"
            assert request(other, "PING")["type"] == "ACK"
            installer.release.set()
            await hub.updates.wait()
        assert len(installer.calls) == hub.link.stops == 1
        assert request(other, "HUB_UPDATE_STATUS", job=job)["phase"] == "done"
        assert hub.link.connected and not hub.updates.maintenance
    asyncio.run(body())


def test_worker_rollback_and_recovery_are_not_reported_as_success(tmp_path):
    async def body():
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        for result, phase in (("rolled_back", "rolled-back"), ("manual_recovery", "recovery-required"), ("failed", "error")):
            hub, sub = setup(tmp_path / result, Installer(result))
            job = upload(sub)
            with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
                request(sub, "HUB_UPDATE_COMMIT", job=job)
                await hub.updates.wait()
            assert hub.updates.allows_local_ping() is (phase == "recovery-required")
            assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == phase
    asyncio.run(body())


async def _finish_with_failed_result_fsync(root):
    """Lose only the final status fsync after the worker has acquired USB."""
    installer = Installer()
    installer.release.clear()
    hub, sub = setup(root, installer)
    job = upload(sub)
    restarts = []
    original_restart = hub._restart_update_link

    def restart():
        restarts.append(hub.link)
        original_restart()

    hub._restart_update_link = restart
    valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
    with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
        assert request(sub, "HUB_UPDATE_COMMIT", job=job)["phase"] == "validating"
        assert await asyncio.to_thread(installer.started.wait, 2)
        await asyncio.sleep(0)
        assert json.loads((root / job / "status.json").read_text())["phase"] == "flashing"
        save = hub.updates._save

        def fail_result_fsync(state):
            if state["phase"] == "done":
                with patch("bosun_hub.update_service.os.fsync", side_effect=OSError("simulated result fsync failure")):
                    save(state)
            else:
                save(state)

        with patch.object(hub.updates, "_save", side_effect=fail_result_fsync):
            installer.release.set()
            await asyncio.wait_for(hub.updates.wait(), 2)
    return hub, sub, job, restarts


def test_final_status_fsync_failure_restarts_link_and_reports_conservative_recovery(tmp_path):
    async def body():
        hub, sub, job, restarts = await _finish_with_failed_result_fsync(tmp_path)
        assert len(restarts) == hub.link.stops == 1
        assert hub.link.connected
        assert not hub.updates.maintenance and hub.updates.active is None
        assert hub.updates.allows_local_ping()
        status = request(sub, "HUB_UPDATE_STATUS", job=job)
        assert status["phase"] == "recovery-required"
        assert "simulated result fsync failure" in status["error"]
        assert status["backup"] == "/saved/full-flash.bin"
        # The volatile result takes precedence over the earlier disk phase,
        # and still blocks another update after releasing ordinary device use.
        assert json.loads((tmp_path / job / "status.json").read_text())["phase"] == "flashing"
        assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
        assert len(hub.updates.installer.calls) == 1
    asyncio.run(body())


def test_failed_final_fsync_remains_blocked_after_process_state_is_lost(tmp_path):
    async def body():
        _, old_client, job, _ = await _finish_with_failed_result_fsync(tmp_path)
        old_client.close()
        restarted, client = setup(tmp_path)
        assert restarted.updates._volatile_status is None
        # Recreate the service using only disk state: loss of its in-memory
        # terminal result must never turn a partial journal into a new upload.
        assert request(client, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
        status = request(client, "HUB_UPDATE_STATUS", job=job)
        assert status["phase"] == "recovery-required"
        assert "restarted" in status["error"]
        assert (tmp_path / job / "package.zip").read_bytes() == b"archive"
        assert json.loads((tmp_path / job / "status.json").read_text())["phase"] == "recovery-required"
        assert restarted.updates.installer.calls == [] and restarted.link.stops == 0
    asyncio.run(body())


def test_shutdown_waits_for_owned_worker_without_restarting_the_link(tmp_path):
    async def body():
        installer = Installer()
        installer.release.clear()
        hub, sub = setup(tmp_path, installer)
        job = upload(sub)
        restarts = []
        hub._restart_update_link = lambda: restarts.append(True)
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
            request(sub, "HUB_UPDATE_COMMIT", job=job)
            assert await asyncio.to_thread(installer.started.wait, 2)
            hub.updates.stop()
            waiter = asyncio.create_task(hub.updates.wait())
            await asyncio.sleep(0)
            assert not waiter.done() and not hub.updates.task.cancelled()
            assert "stopping" in request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["error"]
            assert not hub.link.connected and hub.updates.maintenance
            installer.release.set()
            await asyncio.wait_for(waiter, 2)
        assert restarts == []
        assert len(installer.calls) == hub.link.stops == 1
        assert not hub.link.connected and not hub.updates.maintenance
        assert hub.updates.active is None
        assert json.loads((tmp_path / job / "status.json").read_text())["phase"] == "done"
    asyncio.run(body())


def test_success_is_not_exposed_until_a_fresh_link_reconnects(tmp_path):
    async def body():
        hub, sub = setup(tmp_path)
        old_link = hub.link
        fresh_link = Link()
        fresh_link.connected = False
        restarting = asyncio.Event()

        def restart():
            hub.link = fresh_link
            restarting.set()

        hub._restart_update_link = restart
        job = upload(sub)
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
            request(sub, "HUB_UPDATE_COMMIT", job=job)
            await asyncio.wait_for(restarting.wait(), 2)
            assert hub.link is fresh_link and not fresh_link.connected
            assert hub.updates.maintenance and not hub.updates.task.done()
            assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] not in {"done", "error", "recovery-required"}
            assert request(sub, "PUT_PATCH", patch={})["error"] == "maintenance_busy"
            assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
            fresh_link.connected = True
            await asyncio.wait_for(hub.updates.wait(), 2)
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "done"
        assert old_link.stops == 1 and not old_link.connected
        assert fresh_link.stops == 0 and fresh_link.connected
        assert not hub.updates.maintenance and hub.updates.active is None
    asyncio.run(body())


def test_cancelling_a_shutdown_waiter_does_not_cancel_the_worker(tmp_path):
    async def body():
        installer = Installer()
        installer.release.clear()
        hub, sub = setup(tmp_path, installer)
        job = upload(sub)
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
            request(sub, "HUB_UPDATE_COMMIT", job=job)
            assert await asyncio.to_thread(installer.started.wait, 2)
            waiter = asyncio.create_task(hub.updates.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("The outer waiter should have been cancelled")
            assert not hub.updates.task.done() and hub.updates.maintenance
            installer.release.set()
            await asyncio.wait_for(hub.updates.wait(), 2)
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "done"
        assert len(installer.calls) == hub.link.stops == 1
    asyncio.run(body())


def test_commit_without_transport_usb_identity_refuses_device_ownership(tmp_path):
    async def body():
        hub, sub = setup(tmp_path)
        job = upload(sub)
        hub.link.usb_identity = None
        response = request(sub, "HUB_UPDATE_COMMIT", job=job)
        await hub.updates.wait()
        assert response["type"] == "ERROR"
        assert hub.link.stops == 0 and hub.link.connected
        assert hub.updates.installer.calls == []
        assert not hub.updates.maintenance and hub.updates.task is None
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "uploading"
    asyncio.run(body())


def test_worker_receives_the_usb_identity_of_the_open_transport(tmp_path):
    async def body():
        from bosun_hub.firmware_install import PinnedDevice

        hub, sub = setup(tmp_path)
        expected = PinnedDevice(*hub.link.usb_identity)
        job = upload(sub)
        valid = SimpleNamespace(manifest={"release": "0.6.5", "firmware_version": "0.6.5-native"})
        with patch("bosun_hub.update_package.validate_update_package", return_value=valid):
            request(sub, "HUB_UPDATE_COMMIT", job=job)
            await asyncio.wait_for(hub.updates.wait(), 2)
        assert hub.updates.installer.keyword_calls == [{"expected_device": expected}]
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "done"
    asyncio.run(body())


@pytest.mark.parametrize("phase", ["recovery-required", "validating", "backing-up", "flashing", "rebooting", "verifying"])
def test_restarted_hub_with_captain_in_bootsel_accepts_ping_and_saved_status(tmp_path, phase):
    hub, sub = setup(tmp_path)
    job = upload(sub)
    state = hub.updates._load(job)
    state.update(phase=phase, backup="saved-image.bin")
    hub.updates._save(state)
    restarted, client = setup(tmp_path)
    restarted.link.connected = False
    other = restarted.subscribe()
    assert not restarted.updates.maintenance
    assert request(client, "PING")["type"] == "ACK"
    status = request(client, "HUB_UPDATE_STATUS", job=job)
    assert status["phase"] == "recovery-required"
    assert status["backup"] == "saved-image.bin"
    assert request(client, "GET_DEVICE_INFO")["error"] == "link_down"
    assert restarted.link.sent == [] and other._queue.empty()
    # Heartbeats must not repeatedly walk the Pi filesystem.
    with patch.object(Path, "iterdir", side_effect=AssertionError("Repeated recovery scan")):
        for _ in range(10):
            assert request(client, "PING")["type"] == "ACK"


@pytest.mark.parametrize("phase", [None, "uploading", "done", "error", "rolled-back", "aborted"])
def test_normal_offline_hub_does_not_fake_captain_ping(tmp_path, phase):
    root = tmp_path / "updates"
    hub, sub = setup(root)
    if phase is not None:
        job = upload(sub)
        state = hub.updates._load(job)
        state["phase"] = phase
        hub.updates._save(state)
    restarted, client = setup(root)
    restarted.link.connected = False
    assert request(client, "PING")["error"] == "link_down"
    with patch.object(Path, "iterdir", side_effect=AssertionError("Repeated recovery scan")):
        assert request(client, "PING")["error"] == "link_down"
    assert restarted.link.sent == []
    if phase is None:
        assert not root.exists()


def test_recovery_ping_cache_tracks_finalized_jobs_without_rescanning(tmp_path):
    hub, sub = setup(tmp_path)
    hub.link.connected = False
    assert request(sub, "PING")["error"] == "link_down"
    job = upload(sub)
    state = hub.updates._load(job)
    with patch.object(Path, "iterdir", side_effect=AssertionError("Repeated recovery scan")):
        state["phase"] = "recovery-required"
        hub.updates._save(state)
        assert request(sub, "PING")["type"] == "ACK"
        state["phase"] = "rolled-back"
        hub.updates._save(state)
        assert request(sub, "PING")["error"] == "link_down"


def test_begin_recovers_from_failed_first_status_save_without_removing_evidence(tmp_path):
    hub, sub = setup(tmp_path)
    with patch.object(hub.updates, "_save", side_effect=OSError("simulated disk full")):
        assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    orphan = next(tmp_path.iterdir())
    assert (orphan / "package.zip").exists()
    assert not (orphan / "status.json").exists()
    restarted, client = setup(tmp_path)
    restarted.link.connected = False
    assert request(client, "PING")["error"] == "link_down"
    assert request(client, "HUB_UPDATE_STATUS", job=orphan.name)["phase"] == "aborted"
    assert request(client, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["phase"] == "uploading"
    assert (orphan / "package.zip").exists()  # retained, never deleted implicitly
    assert restarted.link.stops == 0


@pytest.mark.parametrize("evidence", ["installation", "installation/journal.json", "unknown.bin"])
def test_missing_status_with_installation_evidence_requires_recovery(tmp_path, evidence):
    job = "a" * 32
    directory = tmp_path / job
    directory.mkdir()
    (directory / "package.zip").write_bytes(b"original archive")
    artifact = directory / evidence
    if evidence == "installation":
        artifact.mkdir()
    else:
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text('{"flash_may_be_modified":true}')
    hub, sub = setup(tmp_path)
    hub.link.connected = False
    assert request(sub, "PING")["type"] == "ACK"
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "recovery-required"
    assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
    assert artifact.exists() and (directory / "package.zip").read_bytes() == b"original archive"


@pytest.fixture
def recovery_job(tmp_path, monkeypatch):
    from bosun_hub import firmware_install as worker
    if sys.platform == "win32":
        monkeypatch.setattr(worker, "_sync_directory", lambda _: None)
    hub, sub = setup(tmp_path)
    job = upload(sub)
    device = worker.PinnedDevice(*hub.link.usb_identity)
    before = {"info": dict(hub._update_device_info), "active": "live", "profiles": {
        "live": {"metadata": {"id": "live", "color": "blue"}, "device": {"channel": 1},
                 "patches": {"01/01": {"name": "Original"}}, "midi_learn": {}}}}
    installation = tmp_path / job / "installation"
    installation.mkdir()
    journal = {"device": asdict(device), "source_version": before["info"]["fw"],
               "flash_may_be_modified": False, "phase": "manual_recovery"}
    (installation / "journal.json").write_text(json.dumps(journal))
    (installation / "configuration-before.json").write_text(json.dumps(before))
    state = hub.updates._load(job)
    state.update(phase="recovery-required", error="Original BOOTSEL failure")
    hub.updates._save(state)
    hub.updates.active = None

    class RecoveryIO:
        def __init__(self):
            self.calls = []
            self.device = device
            self.after = copy.deepcopy(before)
            self.failure = None
            self.started, self.release = threading.Event(), threading.Event()
            self.release.set()
        def pin(self, port):
            assert port == "/dev/ttyACM1"
            self.calls.append("pin")
            return self.device
        def snapshot(self, pinned, *, save_dirty, expected_info):
            assert pinned == device and save_dirty is False and expected_info == before["info"]
            self.calls.append("snapshot")
            self.started.set()
            self.release.wait(3)
            if self.failure:
                raise worker.FirmwareInstallError(self.failure)
            return self.after
        def __getattr__(self, name):
            raise AssertionError("Recovery cannot mutate device: " + name)
    io = RecoveryIO()
    hub.updates.recovery_verifier = lambda directory, port, **kwargs: worker.verify_prewrite_recovery(
        directory, port, io=io, **kwargs)
    return hub, sub, job, installation, io


def test_public_recovery_verifies_original_configuration_without_writes_and_unblocks_updates(recovery_job):
    async def body():
        hub, sub, job, installation, io = recovery_job
        original = (installation / "journal.json").read_bytes()
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["phase"] == "verifying"
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["phase"] == "verifying"
        assert request(sub, "PING")["type"] == "ACK"
        assert request(sub, "PUT_PATCH", patch={})["error"] == "maintenance_busy"
        await asyncio.wait_for(hub.updates.wait(), 2)
        result = request(sub, "HUB_UPDATE_STATUS", job=job)
        assert result["phase"] == "error" and result["recovery_verified"] is True
        assert result["error"] == "Original BOOTSEL failure"
        assert io.calls == ["pin", "snapshot", "pin"]
        assert Path(result["recovery_record"]).is_file()
        assert (installation / "journal.json").read_bytes() == original
        assert hub.link.connected and hub.link.stops == 1 and hub.link.sent == []
        assert not hub.updates.maintenance and not hub.updates.allows_local_ping()
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["recovery_verified"] is True
        assert hub.link.stops == 1
        assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["phase"] == "uploading"
    asyncio.run(body())


@pytest.mark.parametrize("flag", [True, None, 0, "missing"])
def test_public_recovery_rejects_any_possible_flash_write_before_acquiring_usb(recovery_job, flag):
    hub, sub, job, installation, io = recovery_job
    journal = json.loads((installation / "journal.json").read_text())
    journal["flash_may_be_modified"] = flag
    if flag == "missing":
        journal.pop("flash_may_be_modified")
    (installation / "journal.json").write_text(json.dumps(journal))
    response = request(sub, "HUB_UPDATE_RECOVER", job=job)
    assert response["type"] == "ERROR" and "written flash" in response["error"]
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "recovery-required"
    assert hub.link.stops == 0 and not hub.updates.maintenance and io.calls == []


@pytest.mark.parametrize("failure", ["missing_snapshot", "different_opened_identity", "offline", "another_job"])
def test_public_recovery_admission_preserves_unresolved_job(recovery_job, failure):
    hub, sub, job, installation, io = recovery_job
    if failure == "missing_snapshot":
        (installation / "configuration-before.json").unlink()
    elif failure == "different_opened_identity":
        hub.link.usb_identity = ("/sys/different", "FFFFFFFFFFFFFFFF", "02")
    elif failure == "offline":
        hub.link.connected = False
    else:
        hub.updates.active = "b" * 32
    assert request(sub, "HUB_UPDATE_RECOVER", job=job)["type"] == "ERROR"
    assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "recovery-required"
    assert hub.link.stops == 0 and io.calls == [] and not hub.updates.maintenance


@pytest.mark.parametrize("failure", ["changed_physical_device", "changed_configuration", "changed_firmware", "dirty"])
def test_public_recovery_failure_restarts_link_and_keeps_recovery_required(recovery_job, failure):
    async def body():
        from bosun_hub.firmware_install import PinnedDevice
        hub, sub, job, installation, io = recovery_job
        if failure == "changed_physical_device":
            io.device = PinnedDevice("/sys/different", "FFFFFFFFFFFFFFFF")
        elif failure == "changed_configuration":
            io.after["profiles"]["live"]["device"]["channel"] = 2
        elif failure == "changed_firmware":
            io.after["info"]["fw"] = "different"
        else:
            io.failure = "Unexpected unsaved changes after the update"
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["phase"] == "verifying"
        await asyncio.wait_for(hub.updates.wait(), 2)
        result = request(sub, "HUB_UPDATE_STATUS", job=job)
        assert result["phase"] == "recovery-required" and result["recovery_verified"] is False
        assert result["recovery_error"] and "Recovery check failed" in result["detail"]
        assert not (installation.parent / "recovery-record.json").exists()
        assert hub.link.stops == 1 and hub.link.connected
        assert not hub.updates.maintenance and hub.updates.active is None
        assert request(sub, "PING")["type"] == "ACK"
        assert request(sub, "HUB_UPDATE_BEGIN", size=1, sha256="1" * 64)["type"] == "ERROR"
        if failure == "changed_physical_device":
            assert io.calls == ["pin"]
    asyncio.run(body())


def test_public_recovery_drains_existing_requests_and_survives_editor_disconnect(recovery_job):
    async def body():
        hub, sub, job, _, io = recovery_job
        io.release.clear()
        hub._request_pending["already-accepted"] = object()
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["phase"] == "verifying"
        await asyncio.sleep(0.01)
        assert hub.link.stops == 0 and io.calls == []
        hub._request_pending.clear()
        assert await asyncio.to_thread(io.started.wait, 2)
        sub.close()
        client = hub.subscribe()
        assert request(client, "HUB_UPDATE_STATUS", job=job)["phase"] == "verifying"
        assert request(client, "PING")["type"] == "ACK"
        assert request(client, "HUB_UPDATE_ABORT", job=job)["type"] == "ERROR"
        io.release.set()
        await asyncio.wait_for(hub.updates.wait(), 2)
        assert request(client, "HUB_UPDATE_STATUS", job=job)["recovery_verified"] is True
    asyncio.run(body())


def test_public_recovery_failed_result_fsync_stays_blocked_and_can_be_rechecked(recovery_job):
    async def body():
        hub, sub, job, installation, io = recovery_job
        original_save = hub.updates._save
        def save(state):
            if state.get("recovery_verified") is True:
                raise OSError("Disk full saving final result")
            original_save(state)
        with patch.object(hub.updates, "_save", side_effect=save):
            request(sub, "HUB_UPDATE_RECOVER", job=job)
            await asyncio.wait_for(hub.updates.wait(), 2)
        result = request(sub, "HUB_UPDATE_STATUS", job=job)
        assert result["phase"] == "recovery-required" and not result["recovery_verified"]
        assert (installation.parent / "recovery-record.json").exists()
        io.release.clear()
        io.started.clear()
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["phase"] == "verifying"
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["phase"] == "verifying"
        io.release.set()
        await asyncio.wait_for(hub.updates.wait(), 2)
        assert request(sub, "HUB_UPDATE_STATUS", job=job)["recovery_verified"] is True
        assert len(list(installation.parent.glob("recovery-record*.json"))) == 2
    asyncio.run(body())


def test_public_recovery_shutdown_waiter_cannot_cancel_the_owned_check(recovery_job):
    async def body():
        hub, sub, job, _, io = recovery_job
        io.release.clear()
        request(sub, "HUB_UPDATE_RECOVER", job=job)
        assert await asyncio.to_thread(io.started.wait, 2)
        waiter = asyncio.create_task(hub.updates.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not hub.updates.task.done() and hub.updates.maintenance
        hub.updates.stop()
        assert request(sub, "HUB_UPDATE_RECOVER", job=job)["type"] == "ERROR"
        io.release.set()
        await asyncio.wait_for(hub.updates.wait(), 2)
        assert hub.link.stops == 1 and not hub.link.connected
        assert not hub.updates.maintenance and hub.updates.active is None
        assert hub.updates._load(job)["recovery_verified"] is True
    asyncio.run(body())


def test_public_recovery_does_not_clear_failure_when_restarted_link_opens_another_captain(recovery_job):
    async def body():
        hub, sub, job, installation, _ = recovery_job
        def restart():
            hub.link.connected = True
            hub.link.usb_identity = ("/sys/replaced", "FFFFFFFFFFFFFFFF", "02")
        hub._restart_update_link = restart
        request(sub, "HUB_UPDATE_RECOVER", job=job)
        await asyncio.wait_for(hub.updates.wait(), 2)
        state = request(sub, "HUB_UPDATE_STATUS", job=job)
        assert state["phase"] == "recovery-required" and state["recovery_verified"] is False
        assert "identity changed" in state["recovery_error"]
        assert (installation.parent / "recovery-record.json").exists()
    asyncio.run(body())
