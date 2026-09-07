"""Recovery qualification is read-only on the Captain and preserves evidence."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosun_hub import firmware_install as install


def configuration():
    return {
        "info": {"fw": "0.1.0-native", "native_experimental": True},
        "active": "live",
        "profiles": {
            "live": {
                "metadata": {"id": "live", "name": "Live", "kind": "kemper_player"},
                "device": {"leds": {"brightness": 81}},
                "patches": {"01/01": {"name": "Clean", "bindings": []}},
                "midi_learn": {"pc_to_patch": [{"pc": 2, "bank": 1, "slot": 1}]},
            },
            "spare": {
                "metadata": {"id": "spare", "name": "Spare", "kind": "generic_midi"},
                "device": {"long_press_ms": 777}, "patches": {}, "midi_learn": {},
            },
        },
    }


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class ReadOnlyIO:
    def __init__(self, device, before):
        self.device = device
        self.before = copy.deepcopy(before)
        self.after = copy.deepcopy(before)
        self.pins = [device, device]
        self.calls = []
        self.snapshot_error = None
        self.on_snapshot = lambda: None

    def pin(self, port):
        assert port == "/dev/ttyACM1"
        self.calls.append("pin")
        return self.pins.pop(0)

    def snapshot(self, device, *, save_dirty, expected_info=None):
        assert device == self.device
        assert save_dirty is False
        assert expected_info == self.before["info"]
        self.calls.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        self.on_snapshot()
        return copy.deepcopy(self.after)

    def forbidden(self, *args, **kwargs):
        pytest.fail("Recovery verification must never reboot or access device flash")

    enter_bootloader = recover_bootloader = save_flash = verify = load = reboot = forbidden


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    directory = (tmp_path / "job" / "installation").resolve()
    directory.mkdir(parents=True)
    device = install.PinnedDevice("/sys/devices/pinned-captain", "1234567890ABCDEF")
    before = configuration()
    journal = {"phase": "manual_recovery", "status": "manual_recovery",
               "flash_may_be_modified": False, "source_version": before["info"]["fw"],
               "device": asdict(device), "error": "USB disconnected before backup"}
    write_json(directory / "journal.json", journal)
    write_json(directory / "configuration-before.json", before)
    if sys.platform == "win32":
        monkeypatch.setattr(install, "_sync_directory", lambda _: None)
    return SimpleNamespace(directory=directory, device=device, before=before, journal=journal,
                           io=ReadOnlyIO(device, before))


def verify(recovery, **kwargs):
    return install.verify_prewrite_recovery(
        recovery.directory, "/dev/ttyACM1", io=recovery.io,
        expected_device=kwargs.get("expected_device", recovery.device))


def assert_no_record(recovery):
    assert not list(recovery.directory.parent.glob("recovery-record*.json"))


def test_verified_recovery_publishes_durable_record_without_changing_original_evidence(recovery, monkeypatch):
    original = {p.name: p.read_bytes() for p in recovery.directory.iterdir()}
    synced = []
    original_sync = install._sync_directory
    def sync(directory):
        original_sync(directory)
        synced.append(directory)
    monkeypatch.setattr(install, "_sync_directory", sync)
    assert install.read_prewrite_recovery(recovery.directory) == (recovery.journal, recovery.before)
    assert recovery.io.calls == []
    result = verify(recovery)
    assert result["recovery_verified"] is True
    record_path = Path(result["recovery_record"])
    assert record_path == recovery.directory.parent / "recovery-record.json"
    assert synced == [record_path.parent]
    record = json.loads(record_path.read_text())
    assert record["flash_may_be_modified"] is False
    assert record["original_version_verified"] is True
    assert record["configuration_snapshot_matched"] is True
    assert record["device"] == asdict(recovery.device)
    assert record["after"] == recovery.before
    assert record["verified_at"] > 0
    assert record["journal_sha256"] == hashlib.sha256(original["journal.json"]).hexdigest()
    assert record["configuration_before_sha256"] == hashlib.sha256(original["configuration-before.json"]).hexdigest()
    assert {p.name: p.read_bytes() for p in recovery.directory.iterdir()} == original
    assert recovery.io.calls == ["pin", "snapshot", "pin"]


def test_opened_usb_identity_mismatch_refuses_even_reading_the_device(recovery):
    changed = replace(recovery.device, serial="FFFFFFFFFFFFFFFF")
    with pytest.raises(install.FirmwareInstallError, match="opened USB identity"):
        verify(recovery, expected_device=changed)
    assert recovery.io.calls == []
    assert_no_record(recovery)


@pytest.mark.parametrize("swap_after_snapshot", [False, True])
def test_fresh_usb_identity_swap_before_or_after_snapshot_is_rejected(recovery, swap_after_snapshot):
    recovery.io.pins[int(swap_after_snapshot)] = replace(recovery.device, usb_path="/sys/devices/other-captain")
    with pytest.raises(install.FirmwareInstallError, match="Captain changed"):
        verify(recovery)
    assert recovery.io.calls == (["pin", "snapshot", "pin"] if swap_after_snapshot else ["pin"])
    assert_no_record(recovery)


@pytest.mark.parametrize("changed", ["firmware", "active", "metadata", "device", "patches", "midi_learn"])
def test_returned_firmware_and_every_configuration_component_must_match(recovery, changed):
    after = recovery.io.after
    if changed == "firmware":
        after["info"]["fw"] = "0.6.5-native"
    elif changed == "active":
        after["active"] = "spare"
    else:
        after["profiles"]["spare"][changed]["changed"] = True
    with pytest.raises(install.FirmwareInstallError, match="expected firmware|differs from the backup"):
        verify(recovery)
    assert_no_record(recovery)


def test_same_version_but_different_firmware_family_is_rejected(recovery):
    # The family must independently agree, even if the reported fw is unchanged.
    recovery.before["info"] = {"fw": "0.6.4", "native_experimental": False}
    recovery.journal["source_version"] = "0.6.4"
    write_json(recovery.directory / "journal.json", recovery.journal)
    write_json(recovery.directory / "configuration-before.json", recovery.before)
    recovery.io = ReadOnlyIO(recovery.device, recovery.before)
    recovery.io.after["info"]["native_experimental"] = True
    with pytest.raises(install.FirmwareInstallError, match="Captain changed during"):
        verify(recovery)
    assert_no_record(recovery)


@pytest.mark.parametrize("flag", [True, "missing", 0, None])
def test_only_explicit_false_proves_that_flash_was_not_written(recovery, flag):
    if flag == "missing":
        del recovery.journal["flash_may_be_modified"]
    else:
        recovery.journal["flash_may_be_modified"] = flag
    write_json(recovery.directory / "journal.json", recovery.journal)
    with pytest.raises(install.FirmwareInstallError, match="may have written flash"):
        verify(recovery)
    assert recovery.io.calls == []
    assert_no_record(recovery)


def test_missing_original_snapshot_refuses_all_device_access(recovery):
    (recovery.directory / "configuration-before.json").unlink()
    with pytest.raises(install.FirmwareInstallError, match="evidence is missing"):
        verify(recovery)
    assert recovery.io.calls == []
    assert_no_record(recovery)


def test_unsaved_device_changes_are_not_committed_by_recovery(recovery):
    recovery.io.snapshot_error = install.FirmwareInstallError("Unexpected unsaved changes after the update")
    with pytest.raises(install.FirmwareInstallError, match="unsaved changes"):
        verify(recovery)
    assert recovery.io.calls == ["pin", "snapshot"]
    assert_no_record(recovery)


@pytest.mark.parametrize("changed", ["journal.json", "configuration-before.json"])
def test_original_evidence_changed_while_reading_device_is_not_certified(recovery, changed):
    def change_evidence():
        path = recovery.directory / changed
        evidence = json.loads(path.read_text())
        evidence["changed_during_verification"] = True
        write_json(path, evidence)
    recovery.io.on_snapshot = change_evidence
    with pytest.raises(install.FirmwareInstallError, match="evidence changed during"):
        verify(recovery)
    assert recovery.io.calls == ["pin", "snapshot", "pin"]
    assert_no_record(recovery)


def test_retry_retains_previous_recovery_record_byte_for_byte(recovery):
    first = Path(verify(recovery)["recovery_record"])
    original = first.read_bytes()
    recovery.io.pins = [recovery.device, recovery.device]
    second = Path(verify(recovery)["recovery_record"])
    assert second != first and second.parent == first.parent
    assert first.read_bytes() == original
    assert json.loads(second.read_text())["configuration_snapshot_matched"] is True
    assert sorted(recovery.directory.parent.glob("recovery-record*.json")) == sorted([first, second])


def test_failed_record_directory_fsync_cannot_report_verified_success(recovery, monkeypatch):
    def fail_sync(directory):
        raise OSError("injected recovery record fsync failure")
    monkeypatch.setattr(install, "_sync_directory", fail_sync)
    with pytest.raises(OSError, match="fsync failure"):
        verify(recovery)
    assert recovery.io.calls == ["pin", "snapshot", "pin"]
    assert json.loads((recovery.directory / "journal.json").read_text()) == recovery.journal
