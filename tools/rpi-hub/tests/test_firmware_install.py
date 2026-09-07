"""Fault injection for firmware migration; no serial, USB or flash is opened."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosun_hub import firmware_install as install


def snapshot(firmware="0.6.4"):
    return {
        "info": {"type": "DEVICE_INFO", "fw": firmware,
                 "native_experimental": "native" in firmware},
        "active": "live",
        "profiles": {
            "live": {
                "metadata": {"id": "live", "name": "Live", "kind": "kemper_player", "color": "#123456"},
                "device": {"future_setting": {"preserve": True}, "leds": {"brightness": 81}},
                "patches": {"01/01": {"name": "Clean", "bindings": []}},
                "midi_learn": {"pc_to_patch": [{"pc": 2, "bank": 1, "slot": 1}]},
            },
            "backup": {
                "metadata": {"id": "backup", "name": "Spare", "kind": "generic_midi", "color": None},
                "device": {"long_press_ms": 777}, "patches": {}, "midi_learn": {"pc_to_patch": []},
            },
        },
    }


class FakeIO:
    def __init__(self, *, source="0.6.4", failure=None):
        self.original = bytes(b"O" * install.STORAGE_OFFSET + b"S" * install.STORAGE_BYTES)
        self.flash = bytearray(self.original)
        self.before = snapshot(source)
        self.current_version = source
        self.failure = failure
        self.calls = []
        self.device = install.PinnedDevice("/sys/devices/pinned-captain", "1234567890ABCDEF")
        self.target = install.BootDevice(self.device, self.device.serial, install.FLASH_BYTES, "E0C912D24340")
        self.changed = False
        self.restore_attempt = False

    def hit(self, step):
        self.calls.append(step)
        if self.failure == step:
            self.failure = None
            raise install.FirmwareInstallError("injected " + step)

    def pin(self, serial_port):
        self.hit("pin")
        return self.device

    def snapshot(self, device, *, save_dirty, expected_info=None):
        assert device == self.device
        self.hit("snapshot_before" if save_dirty else "snapshot_after")
        result = copy.deepcopy(self.before)
        result["info"]["fw"] = self.current_version
        result["info"]["native_experimental"] = "native" in self.current_version
        if self.failure == "configuration_mismatch" and self.changed:
            self.failure = None
            result["profiles"]["backup"]["device"]["lost_setting"] = True
        return result

    def enter_bootloader(self, device, family):
        self.hit("bootloader")
        return self.target

    def recover_bootloader(self, device):
        self.hit("recovery_bootloader")
        return self.target

    def save_flash(self, target, destination):
        self.hit("backup")
        destination.write_bytes(self.flash[:-1] if self.failure == "short_backup" else self.flash)

    def verify(self, target, image, address=None):
        if image.name == "full-flash-before.bin":
            self.hit("verify_rollback" if self.restore_attempt else "verify_backup")
            assert address == install.FLASH_BASE
            assert image.read_bytes() == bytes(self.flash)
        elif image.name == "native-storage.bin":
            self.hit("verify_storage")
            assert address == install.FLASH_BASE + install.STORAGE_OFFSET
            assert image.read_bytes() == bytes(self.flash[install.STORAGE_OFFSET:])
        else:
            self.hit("verify_firmware")
            assert address is None
            assert self.flash[:256] == b"N" * 256

    def load(self, target, image, address=None):
        if image.name == "full-flash-before.bin":
            self.restore_attempt = True
            self.hit("load_rollback")
            self.flash[:] = image.read_bytes()
            self.current_version = self.before["info"]["fw"]
            self.changed = False
        elif image.name == "native-storage.bin":
            self.hit("load_storage")
            self.flash[install.STORAGE_OFFSET:] = image.read_bytes()
        else:
            # Simulate failure after a partial erase/program, not merely a
            # command that never changed any bytes on the NOR device.
            self.flash[:256] = b"N" * 256
            self.current_version = "0.7.0-native"
            self.changed = True
            self.hit("load_firmware")

    def reboot(self, target):
        self.hit("reboot")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    package = SimpleNamespace(manifest={"release": "0.7.0", "firmware_version": "0.7.0-native"},
                              firmware_uf2=b"validated UF2", firmware_pages={})
    monkeypatch.setattr(install, "validate_update_package", lambda path: package)
    def extract(raw, destination):
        destination.mkdir()
        return ["active_profile.json", "profiles/live/device.json"]
    def build(config, output, *, builder):
        output.write_bytes(b"L" * install.STORAGE_BYTES)
        return output.read_bytes()
    monkeypatch.setattr(install, "extract_circuitpython_config", extract)
    monkeypatch.setattr(install, "build_native_storage", build)
    # fsync(directory) is a POSIX durability operation, unavailable on Windows
    # hosts running these offline tests. The worker's real Linux path uses it.
    if sys.platform == "win32":
        monkeypatch.setattr(install, "_sync_directory", lambda directory: None)
    return tmp_path


def run_job(root, io, progress=None):
    return install.FirmwareInstaller(converter=Path("/trusted/bosun_storage_image"), io=io).run(
        root / "package.zip", copy.deepcopy(io.before["info"]), "/dev/ttyACM1",
        root / "installation", progress or (lambda state: None))


def test_cp_migration_preserves_every_profile_and_verified_full_backup(environment):
    io = FakeIO()
    states = []
    result = run_job(environment, io, states.append)
    assert result["status"] == "complete"
    assert result["configuration_verified"] is True
    assert Path(result["backup_path"]).read_bytes() == io.original
    assert result["backup_sha256"] == hashlib.sha256(io.original).hexdigest()
    assert bytes(io.flash[install.STORAGE_OFFSET:]) == b"L" * install.STORAGE_BYTES
    assert io.calls.index("verify_backup") < io.calls.index("load_firmware")
    assert io.calls.index("load_firmware") < io.calls.index("load_storage")
    assert io.calls.index("verify_storage") < io.calls.index("reboot")
    journal = json.loads((environment / "installation/journal.json").read_text())
    assert journal == result
    assert all(state["backup_sha256"] == result["backup_sha256"] for state in states if state["flash_may_be_modified"])


def test_native_update_does_not_write_or_convert_existing_filesystem(environment, monkeypatch):
    io = FakeIO(source="0.1.0-native")
    monkeypatch.setattr(install, "extract_circuitpython_config", lambda *_: pytest.fail("FAT extraction on native"))
    result = run_job(environment, io)
    assert result["status"] == "complete"
    assert "load_storage" not in io.calls
    assert "verify_storage" in io.calls
    assert bytes(io.flash[install.STORAGE_OFFSET:]) == io.original[install.STORAGE_OFFSET:]


@pytest.mark.parametrize("failure", ["pin", "snapshot_before", "bootloader", "backup", "short_backup", "verify_backup"])
def test_preflight_or_backup_failure_never_changes_flash(environment, failure):
    io = FakeIO(failure=failure)
    result = run_job(environment, io)
    assert result["status"] == "failed"
    assert result["flash_may_be_modified"] is False
    assert not any(call.startswith("load_") for call in io.calls)
    assert bytes(io.flash) == io.original


@pytest.mark.parametrize("operation", ["extract_circuitpython_config", "build_native_storage"])
def test_incompatible_or_full_storage_aborts_before_first_flash(environment, monkeypatch, operation):
    io = FakeIO()
    def fail(*args, **kwargs):
        raise ValueError("incompatible configuration or insufficient space")
    monkeypatch.setattr(install, operation, fail)
    result = run_job(environment, io)
    assert result["status"] == "failed"
    assert "load_firmware" not in io.calls
    assert bytes(io.flash) == io.original
    assert Path(result["backup_path"]).read_bytes() == io.original


@pytest.mark.parametrize("failure", ["load_firmware", "verify_firmware", "load_storage", "verify_storage", "reboot", "snapshot_after", "configuration_mismatch"])
def test_partial_write_or_verification_failure_restores_entire_original_flash(environment, failure):
    io = FakeIO(failure=failure)
    result = run_job(environment, io)
    assert result["status"] == "rolled_back", result
    assert result["rollback_verified"] is True
    assert bytes(io.flash) == io.original
    assert "load_rollback" in io.calls
    assert "verify_rollback" in io.calls


@pytest.mark.parametrize("failure", ["recovery_bootloader", "load_rollback", "verify_rollback"])
def test_failed_recovery_keeps_backup_and_reports_manual_recovery(environment, failure):
    io = FakeIO(failure="configuration_mismatch")
    def progress(state):
        if state["phase"] == "rolling_back":
            io.failure = failure
    result = run_job(environment, io, progress)
    assert result["status"] == "manual_recovery"
    assert result["rollback_verified"] is False
    assert Path(result["backup_path"]).read_bytes() == io.original
    assert "configuration_verified" not in result


def test_corrupted_backup_is_never_used_for_rollback(environment):
    io = FakeIO(failure="configuration_mismatch")
    def progress(state):
        if state["phase"] == "rolling_back":
            Path(state["backup_path"]).write_bytes(b"corrupt")
    result = run_job(environment, io, progress)
    assert result["status"] == "manual_recovery"
    assert "load_rollback" not in io.calls
    assert "hash" in result["recovery_error"]


def test_wrong_bootloader_identity_blocks_all_flash_access(environment):
    io = FakeIO()
    io.target = install.BootDevice(io.device, "FFFFFFFFFFFFFFFF", install.FLASH_BYTES, "E0C912D24340")
    result = run_job(environment, io)
    assert result["status"] == "failed"
    assert "backup" not in io.calls
    assert "load_firmware" not in io.calls


def test_ui_disconnect_cannot_interrupt_update_or_rollback(environment):
    io = FakeIO()
    def disconnected(_):
        raise ConnectionError("UI disconnected")
    assert run_job(environment, io, disconnected)["status"] == "complete"


def test_power_loss_leaves_recoverable_journal_before_first_write(environment):
    class PowerLoss(BaseException):
        pass
    io = FakeIO()
    normal_load = io.load
    def interrupted(target, image, address=None):
        normal_load(target, image, address)
        raise PowerLoss()
    io.load = interrupted
    with pytest.raises(PowerLoss):
        run_job(environment, io)
    journal = json.loads((environment / "installation/journal.json").read_text())
    assert journal["status"] == "running"
    assert journal["phase"] == "flashing_firmware"
    assert journal["flash_may_be_modified"] is True
    assert Path(journal["backup_path"]).read_bytes() == io.original
    assert journal["backup_sha256"] == hashlib.sha256(io.original).hexdigest()


def test_existing_job_is_never_overwritten(environment):
    (environment / "installation").mkdir()
    marker = environment / "installation/keep.bin"
    marker.write_bytes(b"previous recovery data")
    with pytest.raises(FileExistsError):
        run_job(environment, FakeIO())
    assert marker.read_bytes() == b"previous recovery data"


@pytest.mark.parametrize("unique_id,size", [("FFFFFFFFFFFFFFFF", "8192K"), ("1234567890ABCDEF", "2048K")])
def test_picotool_rejects_wrong_physical_identity_or_capacity(tmp_path, monkeypatch, unique_id, size):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    for name, value in {"idVendor": "2e8a", "idProduct": "0003", "busnum": "1", "devnum": "19", "serial": "E0C912D24340"}.items():
        (tmp_path / name).write_text(value)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_command", lambda *args, **kwargs: f"Device Information\n flash size: {size}\n flash id: 0x{unique_id}\n")
    with pytest.raises(install.FirmwareInstallError, match="identity/flash capacity"):
        io._boot_device(device, timeout=0)


def test_picotool_uses_fresh_pinned_bus_address_for_every_operation(tmp_path, monkeypatch):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    target = install.BootDevice(device, device.serial, install.FLASH_BYTES, "E0C912D24340")
    io = object.__new__(install.LinuxInstallIO)
    calls = []
    monkeypatch.setattr(io, "_target_arguments", lambda _: ["--bus", "1", "--address", "19", "--ser", target.unique_id])
    monkeypatch.setattr(io, "_command", lambda arguments, **kwargs: calls.append(arguments))
    image = tmp_path / "full backup.bin"
    image.write_bytes(b"image")
    io.load(target, image, install.FLASH_BASE)
    io.verify(target, image, install.FLASH_BASE)
    assert calls == [
        ["load", "-v", str(image), "-o", "0x10000000", "--bus", "1", "--address", "19", "--ser", target.unique_id],
        ["verify", str(image), "-o", "0x10000000", "--bus", "1", "--address", "19", "--ser", target.unique_id],
    ]


def test_full_flash_operations_allow_slow_pi_usb_and_do_not_duplicate_backup_verification(tmp_path, monkeypatch):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    target = install.BootDevice(device, device.serial, install.FLASH_BYTES, "E0C912D24340")
    io = object.__new__(install.LinuxInstallIO)
    calls = []
    selectors = ["--bus", "1", "--address", "19", "--ser", device.serial]
    monkeypatch.setattr(io, "_target_arguments", lambda _: selectors)
    def command(arguments, **kwargs):
        calls.append((arguments, kwargs["timeout"]))
        if arguments[0] == "save":
            with Path(arguments[4]).open("wb") as out:
                out.truncate(int(arguments[3], 16) - int(arguments[2], 16))
    monkeypatch.setattr(io, "_command", command)
    image = tmp_path / "full backup.bin"
    io.save_flash(target, image)
    assert image.stat().st_size == install.FLASH_BYTES
    io.verify(target, image, install.FLASH_BASE)
    io.load(target, image, install.FLASH_BASE)
    assert calls[:8] == [
        (["save", "-r", hex(install.FLASH_BASE + offset), hex(install.FLASH_BASE + offset + 1024 * 1024),
          str(image.with_name(image.name + ".parts") / f"{offset:08x}.bin"), *selectors], 180)
        for offset in range(0, install.FLASH_BYTES, 1024 * 1024)
    ]
    assert calls[8:] == [
        (["verify", str(image), "-o", "0x10000000", *selectors], 600),
        (["load", "-v", str(image), "-o", "0x10000000", *selectors], 600),
    ]
    image.write_bytes(b"small firmware or storage image")
    io.load(target, image)
    io.verify(target, image)
    assert [timeout for _, timeout in calls[-2:]] == [180, 180]


@pytest.fixture
def range_backup(tmp_path, monkeypatch):
    usb = tmp_path / "usb"
    usb.mkdir()
    values = {"idVendor": "2e8a", "idProduct": "0003", "busnum": "1", "devnum": "19", "serial": "E0C912D24340"}
    for name, value in values.items():
        (usb / name).write_text(value)
    device = install.PinnedDevice(str(usb), "1234567890ABCDEF")
    target = install.BootDevice(device, device.serial, install.FLASH_BYTES, values["serial"])
    io = object.__new__(install.LinuxInstallIO)
    calls = []
    data = [bytes((index * 17 + byte) % 256 for byte in range(256)) * 4096 for index in range(8)]
    def command(arguments, **kwargs):
        calls.append((arguments, kwargs["timeout"]))
        assert arguments[-6:] == ["--bus", "1", "--address", (usb / "devnum").read_text(), "--ser", device.serial]
        if arguments[0] == "info":
            return f"Device Information\n flash size: 8192K\n flash id: 0x{device.serial}\n"
        assert arguments[:2] == ["save", "-r"]
        start, end = int(arguments[2], 16), int(arguments[3], 16)
        index = (start - install.FLASH_BASE) // (1024 * 1024)
        assert (start, end) == (install.FLASH_BASE + index * 1024 * 1024, install.FLASH_BASE + (index + 1) * 1024 * 1024)
        Path(arguments[4]).write_bytes(data[index])
        # A new USB address is permitted only after freshly checking the same
        # physical port, flash UID, capacity and ROM descriptor next time.
        (usb / "devnum").write_text(str(20 + index))
        return ""
    monkeypatch.setattr(io, "_command", command)
    return SimpleNamespace(io=io, target=target, calls=calls, data=data, command=command,
                           destination=tmp_path / "full flash.bin", usb=usb)


def test_range_backup_assembles_all_bytes_in_flash_order_and_rechecks_each_target(range_backup):
    fixture = range_backup
    fixture.io.save_flash(fixture.target, fixture.destination)
    assert fixture.destination.read_bytes() == b"".join(fixture.data)
    assert [args[0] for args, _ in fixture.calls] == ["info", "save"] * 8
    saves = [args for args, _ in fixture.calls if args[0] == "save"]
    assert [args[-3] for args in saves] == [str(number) for number in range(19, 27)]
    assert all("-a" not in args and "-v" not in args for args in saves)
    parts = fixture.destination.with_name(fixture.destination.name + ".parts")
    assert sum(part.stat().st_size for part in parts.iterdir()) == install.FLASH_BYTES


@pytest.mark.parametrize("fault", ["missing", "short", "large", "directory", "command"])
def test_range_backup_rejects_failed_or_invalid_fragment_and_retains_partial_evidence(range_backup, monkeypatch, fault):
    fixture = range_backup
    def command(arguments, **kwargs):
        result = fixture.command(arguments, **kwargs)
        if arguments[0] == "save" and int(arguments[2], 16) == install.FLASH_BASE + 2 * 1024 * 1024:
            part = Path(arguments[4])
            if fault == "command":
                raise install.FirmwareInstallError("injected USB read failure")
            if fault in ("missing", "directory"):
                part.unlink()
                if fault == "directory":
                    part.mkdir()
            else:
                with part.open("r+b") as output:
                    output.truncate(1024 * 1024 + (-1 if fault == "short" else 1))
        return result
    monkeypatch.setattr(fixture.io, "_command", command)
    with pytest.raises(install.FirmwareInstallError, match="Flash backup range 3/8 failed") as failure:
        fixture.io.save_flash(fixture.target, fixture.destination)
    assert fixture.destination.read_bytes() == b"".join(fixture.data[:2])
    assert [args[0] for args, _ in fixture.calls] == ["info", "save"] * 3
    if fault == "command":
        assert "injected USB read failure" in str(failure.value)


@pytest.mark.parametrize("changed", ["uid", "capacity", "rom", "physical"])
def test_range_backup_stops_on_identity_change_before_next_read(range_backup, monkeypatch, changed):
    fixture = range_backup
    def command(arguments, **kwargs):
        result = fixture.command(arguments, **kwargs)
        reads = sum(args[0] == "save" for args, _ in fixture.calls)
        if reads == 2:
            if arguments[0] == "info":
                if changed == "uid":
                    return result.replace(fixture.target.unique_id, "FFFFFFFFFFFFFFFF")
                if changed == "capacity":
                    return result.replace("8192K", "4096K")
            if arguments[0] == "save":
                if changed == "rom":
                    (fixture.usb / "serial").write_text("E0C912D24341")
                if changed == "physical":
                    monkeypatch.setattr(fixture.io, "_read", lambda _: (_ for _ in ()).throw(FileNotFoundError()))
                    monkeypatch.setattr(install.time, "sleep", lambda _: None)
                    ticks = iter([0, 0, 1, 2, 3, 4])
                    monkeypatch.setattr(install.time, "monotonic", lambda: next(ticks, 601))
        return result
    monkeypatch.setattr(fixture.io, "_command", command)
    with pytest.raises(install.FirmwareInstallError, match="Flash backup range 3/8 failed"):
        fixture.io.save_flash(fixture.target, fixture.destination)
    assert fixture.destination.read_bytes() == b"".join(fixture.data[:2])
    assert sum(args[0] == "save" for args, _ in fixture.calls) == 2


@pytest.mark.parametrize("existing", ["backup", "parts"])
def test_range_backup_never_overwrites_existing_artifacts(range_backup, existing):
    fixture = range_backup
    if existing == "backup":
        marker = fixture.destination
    else:
        parts = fixture.destination.with_name(fixture.destination.name + ".parts")
        parts.mkdir()
        marker = parts / "00000000.bin"
    marker.write_bytes(b"keep previous recovery evidence")
    with pytest.raises((install.FirmwareInstallError, FileExistsError)):
        fixture.io.save_flash(fixture.target, fixture.destination)
    assert marker.read_bytes() == b"keep previous recovery evidence"
    assert fixture.calls == []


def test_range_backup_rejects_unknown_geometry_before_creating_backup(range_backup):
    fixture = range_backup
    wrong = install.BootDevice(fixture.target.device, fixture.target.unique_id, 4 * 1024 * 1024, fixture.target.rom_serial)
    with pytest.raises(install.FirmwareInstallError, match="8 MiB Captain geometry"):
        fixture.io.save_flash(wrong, fixture.destination)
    assert not fixture.destination.exists()
    assert fixture.calls == []


def test_range_backup_total_time_budget_includes_fresh_identity_checks(range_backup, monkeypatch):
    fixture = range_backup
    now = [0]
    monkeypatch.setattr(install.time, "monotonic", lambda: now[0])
    def command(arguments, **kwargs):
        cost = 15 if arguments[0] == "info" else 80
        if cost > kwargs["timeout"]:
            now[0] += kwargs["timeout"]
            raise install.subprocess.TimeoutExpired("picotool save", kwargs["timeout"])
        now[0] += cost
        return fixture.command(arguments, **kwargs)
    monkeypatch.setattr(fixture.io, "_command", command)
    with pytest.raises(install.FirmwareInstallError, match="Flash backup range 7/8 failed"):
        fixture.io.save_flash(fixture.target, fixture.destination)
    assert now[0] == 600
    assert fixture.destination.read_bytes() == b"".join(fixture.data[:6])


@pytest.mark.parametrize("fault", ["command", "short", "verify"])
def test_chunked_backup_failure_remains_prewrite_and_never_starts_flash_rollback(environment, monkeypatch, fault):
    io = FakeIO(source="0.1.0-native", failure="verify_backup" if fault == "verify" else None)
    linux = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(linux, "_target_arguments", lambda target: ["--ser", target.unique_id])
    def command(arguments, **kwargs):
        start, end = int(arguments[2], 16) - install.FLASH_BASE, int(arguments[3], 16) - install.FLASH_BASE
        data = io.original[start:end]
        if start == 2 * 1024 * 1024 and fault == "command":
            raise install.FirmwareInstallError("injected chunk read failure")
        if start == 2 * 1024 * 1024 and fault == "short":
            data = data[:-1]
        Path(arguments[4]).write_bytes(data)
    monkeypatch.setattr(linux, "_command", command)
    monkeypatch.setattr(io, "save_flash", linux.save_flash)
    states = []
    result = run_job(environment, io, states.append)
    assert result["status"] == "failed"
    assert result["flash_may_be_modified"] is False
    assert "backup_sha256" not in result
    assert not any(call.startswith("load_") for call in io.calls)
    assert "reboot" in io.calls
    assert bytes(io.flash) == io.original
    assert all(not state["flash_may_be_modified"] for state in states)
    assert ("verify_backup" in io.calls) == (fault == "verify")


@pytest.mark.parametrize("dirty", [True, False])
def test_readonly_snapshot_never_saves_or_changes_profiles(monkeypatch, dirty):
    expected = snapshot()
    calls = []
    class Client:
        def __init__(self, port):
            assert port == "/dev/pinned-captain"
        def close(self):
            pass
        def request(self, command, response, **fields):
            calls.append(command)
            if command == "GET_DEVICE_INFO":
                return expected["info"]
            if command == "GET_DIRTY":
                return {"patches": [{"bank": 1, "slot": 1}] if dirty else []}
            if command == "LIST_PROFILES":
                return {"active": expected["active"], "profiles": [
                    {**row["metadata"], "active": key == expected["active"]}
                    for key, row in expected["profiles"].items()]}
            row = expected["profiles"][fields["profile"]]
            data = {"type": response, "profile": fields["profile"]}
            if command == "GET_GLOBAL":
                return {**data, "device": row["device"]}
            if command == "GET_MIDI_LEARN":
                return {**data, "table": row["midi_learn"]}
            if command == "LIST_PATCHES":
                return {**data, "patches": [{"bank": int(key[:2]), "slot": int(key[3:])} for key in row["patches"]]}
            if command == "GET_PATCH":
                key = f"{fields['bank']:02}/{fields['slot']:02}"
                return {**data, "bank": fields["bank"], "slot": fields["slot"], "patch": row["patches"][key]}
            pytest.fail("Unexpected device mutation: " + command)
    monkeypatch.setattr(install, "_ProtocolClient", Client)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_open_runtime_client", lambda _: Client("/dev/pinned-captain"))
    device = install.PinnedDevice("/sys/pinned", "1234567890ABCDEF")
    if dirty:
        with pytest.raises(install.FirmwareInstallError, match="Save edits"):
            io.snapshot(device, save_dirty=True, expected_info=expected["info"])
        assert calls == ["GET_DEVICE_INFO", "GET_DIRTY"]
    else:
        assert io.snapshot(device, save_dirty=True, expected_info=expected["info"]) == expected
        assert set(calls) <= {"GET_DEVICE_INFO", "GET_DIRTY", "LIST_PROFILES", "GET_GLOBAL", "LIST_PATCHES", "GET_PATCH", "GET_MIDI_LEARN"}


def test_changed_firmware_is_rejected_at_first_readonly_response(monkeypatch):
    calls = []
    class Client:
        def __init__(self, port):
            pass
        def close(self):
            pass
        def request(self, command, response, **fields):
            calls.append(command)
            return {"type": "DEVICE_INFO", "fw": "0.1.0-native", "native_experimental": True}
    monkeypatch.setattr(install, "_ProtocolClient", Client)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_open_runtime_client", lambda _: Client("/dev/pinned-captain"))
    with pytest.raises(install.FirmwareInstallError, match="firmware changed"):
        io.snapshot(install.PinnedDevice("/sys/pinned", "1234567890ABCDEF"),
                    save_dirty=True, expected_info={"fw": "0.6.4"})
    assert calls == ["GET_DEVICE_INFO"]


@pytest.mark.parametrize("failure", [OSError("USB disconnected"), install.FirmwareResponseTimeout("ACK lost")])
@pytest.mark.parametrize("boot_matches", [True, False])
def test_native_reboot_ack_loss_requires_verified_bootsel(monkeypatch, failure, boot_matches):
    calls = []
    device = install.PinnedDevice("/sys/pinned", "1234567890ABCDEF")
    target = install.BootDevice(device, device.serial, install.FLASH_BYTES, "E0C912D24340")
    class Client:
        def __init__(self, port): pass
        def close(self): calls.append("close")
        def request(self, command, response, **fields):
            calls.append(command)
            if command == "REBOOT": raise failure
            return {"fw": "0.1.0-native", "reboot_modes": ["bootloader"]}
    monkeypatch.setattr(install, "_ProtocolClient", Client)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_open_runtime_client", lambda _: Client("/dev/pinned"))
    def boot(actual):
        assert actual == device and calls[-1] == "close"
        if not boot_matches: raise install.FirmwareInstallError("Pinned device not in BOOTSEL")
        return target, []
    monkeypatch.setattr(io, "_boot_device", boot)
    if boot_matches:
        assert io.enter_bootloader(device, "native") == target
    else:
        with pytest.raises(install.FirmwareInstallError, match="Pinned device"):
            io.enter_bootloader(device, "native")
    assert calls == ["GET_DEVICE_INFO", "REBOOT", "close"]


def test_native_reboot_refusal_is_not_treated_as_ack_loss(monkeypatch):
    class Client:
        def __init__(self, port): pass
        def close(self): pass
        def request(self, command, response, **fields):
            if command == "REBOOT": raise install.FirmwareInstallError("REBOOT failed: refused")
            return {"fw": "0.1.0-native", "reboot_modes": ["bootloader"]}
    monkeypatch.setattr(install, "_ProtocolClient", Client)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_open_runtime_client", lambda _: Client("/dev/pinned"))
    monkeypatch.setattr(io, "_boot_device", lambda _: pytest.fail("Explicit refusal must not be ignored"))
    with pytest.raises(install.FirmwareInstallError, match="refused"):
        io.enter_bootloader(install.PinnedDevice("/sys/pinned", "1234567890ABCDEF"), "native")


def test_full_state_disk_after_first_flash_write_does_not_prevent_rollback(environment, monkeypatch):
    io = FakeIO()
    normal_write = install._write_new
    unavailable = False
    def full_disk(path, data):
        nonlocal unavailable
        unavailable = unavailable or io.changed
        if unavailable and path.name.startswith(".journal-"):
            raise OSError("state disk is full")
        normal_write(path, data)
    monkeypatch.setattr(install, "_write_new", full_disk)
    result = run_job(environment, io)
    assert result["status"] == "rolled_back"
    assert result["rollback_verified"] is True
    assert "state disk is full" in result["journal_error"]
    assert "load_rollback" in io.calls
    assert bytes(io.flash) == io.original
    # The last durable record remains conservative and contains the verified
    # recovery image even when no later journal update can reach the SD card.
    last_durable = json.loads((environment / "installation/journal.json").read_text())
    assert last_durable["flash_may_be_modified"] is True
    assert Path(last_durable["backup_path"]).read_bytes() == io.original


def test_failed_return_from_bootsel_before_flashing_requires_recovery(environment, monkeypatch):
    io = FakeIO()
    def incompatible(*args, **kwargs):
        io.failure = "recovery_bootloader"
        raise ValueError("unsupported configuration")
    monkeypatch.setattr(install, "extract_circuitpython_config", incompatible)
    result = run_job(environment, io)
    assert result["status"] == "manual_recovery"
    assert result["flash_may_be_modified"] is False
    assert "recovery_bootloader" in result["recovery_error"]
    assert "load_firmware" not in io.calls


def test_reassigned_tty_cannot_replace_the_device_pinned_by_the_hub(environment):
    io = FakeIO()
    previously_owned = install.PinnedDevice(io.device.usb_path, "FFFFFFFFFFFFFFFF")
    result = install.FirmwareInstaller(converter=Path("/trusted/builder"), io=io).run(
        environment / "package.zip", io.before["info"], "/dev/ttyACM1", environment / "installation",
        lambda state: None, expected_device=previously_owned)
    assert result["status"] == "failed"
    assert "USB device changed" in result["error"]
    assert io.calls == ["pin"]


def test_real_linux_installer_requires_identity_captured_by_the_owned_link(environment, monkeypatch):
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "pin", lambda _: pytest.fail("Late pinning without an owned-link anchor"))
    result = install.FirmwareInstaller(converter=Path("/trusted/builder"), io=io).run(
        environment / "package.zip", {"fw": "0.6.4"}, "/dev/ttyACM1", environment / "installation",
        lambda state: None)
    assert result["status"] == "failed"
    assert "physical USB identity" in result["error"]


@pytest.mark.parametrize("case", ["plugin", "message", "switch", "duplicate", "mode", "action",
                                  "macro", "transition", "expression", "channel", "cc", "navigation"])
def test_behavior_incompatible_cp_configuration_is_rejected_before_bootsel(environment, case):
    io = FakeIO()
    profile = io.before["profiles"]["live"]
    patch = profile["patches"]["01/01"]
    midi = {"type": "cc", "channel": 1, "cc": 7, "value": 1}
    binding = {"switch": "1", "mode": "tap", "actions": {"press": {"messages": [midi]}}}
    patch["bindings"] = [binding]
    if case == "plugin": profile["metadata"]["kind"] = "ampero_ii_stage"
    elif case == "message": midi["type"] = "unsupported_future_type"
    elif case == "switch": binding["switch"] = "5"
    elif case == "duplicate": patch["bindings"].append(copy.deepcopy(binding))
    elif case == "mode": binding["mode"] = "unsupported_mode"
    elif case == "action": binding["actions"]["held"] = {"messages": [midi]}
    elif case == "macro": binding["actions"]["press"]["messages"] = [midi] * 129
    elif case == "transition":
        patch["on_enter"] = {"messages": [midi] * 65}
        patch["on_exit"] = {"messages": [midi] * 64}
    elif case == "expression":
        profile["device"]["expression"] = [{"jack": 1, "enabled": True, "message": {"type": "pc"}}]
    elif case == "channel": midi["channel"] = 17
    elif case == "cc": midi["cc"] = 128
    elif case == "navigation":
        midi["type"] = "captain_bank_step"
        profile["patches"].update({f"{i // 10 + 1:02}/{i % 10 + 1:02}": {"bindings": []} for i in range(1, 129)})
    result = run_job(environment, io)
    assert result["status"] == "failed", result
    assert "CircuitPython was left unchanged" in result["error"]
    assert "bootloader" not in io.calls
    assert "backup" not in io.calls
    assert bytes(io.flash) == io.original


def test_supported_kemper_actions_global_long_press_and_expression_are_accepted(environment):
    io = FakeIO()
    profile = io.before["profiles"]["live"]
    profile["device"].update({
        "kemper": {},
        "long_press_actions": {"1": [{"type": "kemper_tuner", "state": "on"}]},
        "expression": [{"jack": 1, "enabled": True, "curve": "log", "message": {"type": "kemper_wah", "value": 0}}],
    })
    profile["patches"]["01/01"]["bindings"] = [{
        "switch": "1", "mode": "latched", "actions": {
            "toggle_on": {"messages": [{"type": "kemper_effect_toggle", "slot": "X", "value": "on"}]},
            "toggle_off": {"messages": [{"type": "kemper_effect_toggle", "slot": "X", "value": "off"}]},
        },
    }]
    assert run_job(environment, io)["status"] == "complete"


def test_cp_preflight_limits_and_names_match_the_native_runtime_source():
    root = Path(__file__).resolve().parents[3]
    runtime = (root / "firmware-native/src/runtime.c").read_text(encoding="utf-8")
    header = (root / "firmware-native/include/bosun/runtime.h").read_text(encoding="utf-8")
    for name, actual in (("supported", install.NATIVE_MESSAGES), ("switch_names", install.NATIVE_SWITCHES),
                         ("mode_names", install.NATIVE_MODES), ("action_names", install.NATIVE_ACTIONS)):
        body = re.search(r"\b" + name + r"\[[^]]*\]\s*=\s*\{([^}]+)\}", runtime).group(1)
        assert set(re.findall(r'"([^"]+)"', body)) == actual
    assert int(re.search(r"#define BOSUN_RUNTIME_COMMANDS (\d+)u", header).group(1)) == install.NATIVE_COMMAND_LIMIT
    assert int(re.search(r"#define BOSUN_RUNTIME_NAV_PATCHES (\d+)u", header).group(1)) == install.NATIVE_NAVIGATION_LIMIT


def test_rp2040_picotool_serial_filter_uses_flash_uid_not_rom_usb_serial(tmp_path, monkeypatch):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    rom_serial = "E0C912D24340"
    for name, value in {"idVendor": "2e8a", "idProduct": "0003", "busnum": "1", "devnum": "19", "serial": rom_serial}.items():
        (tmp_path / name).write_text(value)
    io = object.__new__(install.LinuxInstallIO)
    calls = []
    def command(arguments, **kwargs):
        calls.append(arguments)
        # picoboot_open_device() in picotool 2.1.1 parses --ser as a
        # hexadecimal uint64 and compares picoboot_flash_id() on RP2040.
        # The ROM USB descriptor is deliberately not used: it is not unique.
        selected_uid = arguments[arguments.index("--ser") + 1]
        if int(selected_uid, 16) != int(device.serial, 16):
            raise install.FirmwareInstallError("picotool failed: No accessible RP-series devices in BOOTSEL mode were found.")
        return "Device Information\n flash size: 8192K\n flash id: 0x1234567890ABCDEF\n"
    monkeypatch.setattr(io, "_command", command)
    target, arguments = io._boot_device(device, timeout=0)
    assert target.unique_id == device.serial
    assert target.rom_serial == rom_serial
    assert target.rom_serial != target.unique_id
    assert arguments == ["--bus", "1", "--address", "19", "--ser", device.serial]
    assert calls == [["info", "-a", *arguments]]


def test_changed_rom_serial_blocks_next_flash_command_even_with_same_flash_id(tmp_path, monkeypatch):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    original = install.BootDevice(device, device.serial, install.FLASH_BYTES, "E0C912D24340")
    changed = install.BootDevice(device, device.serial, install.FLASH_BYTES, "E0C912D24341")
    io = object.__new__(install.LinuxInstallIO)
    commands = []
    monkeypatch.setattr(io, "_boot_device", lambda *args, **kwargs: (changed, ["--ser", changed.unique_id]))
    monkeypatch.setattr(io, "_command", lambda *args, **kwargs: commands.append(args))
    with pytest.raises(install.FirmwareInstallError, match="target changed"):
        io.load(original, tmp_path / "firmware.uf2")
    assert commands == []


@pytest.mark.parametrize("rom_serial", ["1234567890ABCDEF", "not-a-serial", ""])
def test_runtime_or_malformed_serial_is_not_accepted_as_a_rom_descriptor(tmp_path, monkeypatch, rom_serial):
    device = install.PinnedDevice(str(tmp_path), "1234567890ABCDEF")
    for name, value in {"idVendor": "2e8a", "idProduct": "0003", "busnum": "1", "devnum": "19", "serial": rom_serial}.items():
        (tmp_path / name).write_text(value)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_command", lambda *args, **kwargs: pytest.fail("Invalid ROM serial reached picotool"))
    with pytest.raises(install.FirmwareInstallError, match="ROM USB serial"):
        io._boot_device(device, timeout=0)


@pytest.fixture
def runtime_io(tmp_path, monkeypatch):
    io = object.__new__(install.LinuxInstallIO)
    io.sysfs, io.dev = tmp_path / "sys", tmp_path / "dev"
    (io.sysfs / "class/tty/ttyACM1").mkdir(parents=True)
    io.dev.mkdir()
    (io.dev / "ttyACM1").write_bytes(b"")
    usb = io.sysfs / "usb-captain"
    usb.mkdir()
    for name, value in {"idVendor": "239a", "idProduct": "80f4", "serial": "1234567890ABCDEF"}.items():
        (usb / name).write_text(value)
    device = install.PinnedDevice(str(usb), "1234567890ABCDEF")
    monkeypatch.setattr(io, "_usb_parent", lambda _: (usb, "02"))
    elapsed = [0.0]
    monkeypatch.setattr(install.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(install.time, "sleep", lambda duration: elapsed.__setitem__(0, elapsed[0] + duration))
    return io, device, elapsed


def test_runtime_port_waits_for_udev_read_and_write_permissions(runtime_io, monkeypatch):
    io, device, elapsed = runtime_io
    attempts = []
    def accessible(path, mode):
        assert path == str(io.dev / "ttyACM1")
        assert mode == install.os.R_OK | install.os.W_OK
        attempts.append(path)
        return len(attempts) >= 3
    monkeypatch.setattr(install.os, "access", accessible)
    assert io._runtime_port(device, timeout=1) == str(io.dev / "ttyACM1")
    assert len(attempts) == 3 and elapsed[0] == pytest.approx(0.3)


def test_runtime_permissions_timeout_is_bounded(runtime_io, monkeypatch):
    io, device, elapsed = runtime_io
    monkeypatch.setattr(install.os, "access", lambda *_: False)
    with pytest.raises(install.FirmwareInstallError, match="accessible"):
        io._runtime_port(device, timeout=0.2)
    assert elapsed[0] <= 0.3


@pytest.mark.parametrize("error_number", [install.errno.EACCES, install.errno.ENOENT])
def test_runtime_open_retries_permission_or_disappearance_race_with_fresh_pin(runtime_io, monkeypatch, error_number):
    io, device, _ = runtime_io
    attempts = []
    client = SimpleNamespace(close=lambda: pytest.fail("Successful client was closed"))
    def open_client(port):
        attempts.append(port)
        if len(attempts) == 1:
            raise OSError(error_number, "udev has not finished")
        return client
    monkeypatch.setattr(install, "_ProtocolClient", open_client)
    assert io._open_runtime_client(device, timeout=1) is client
    assert len(attempts) == 2


def test_runtime_open_permission_failure_does_not_retry_forever(runtime_io, monkeypatch):
    io, device, elapsed = runtime_io
    monkeypatch.setattr(install, "_ProtocolClient", lambda _: (_ for _ in ()).throw(PermissionError(13, "not ready")))
    with pytest.raises(install.FirmwareInstallError, match="accessible"):
        io._open_runtime_client(device, timeout=0.2)
    assert elapsed[0] <= 0.3


@pytest.mark.parametrize("changed_after_open", [False, True])
def test_runtime_identity_swap_stops_retries_and_closes_open_client(runtime_io, monkeypatch, changed_after_open):
    io, device, _ = runtime_io
    calls = []
    def open_client(port):
        calls.append("open")
        (Path(device.usb_path) / "serial").write_text("FFFFFFFFFFFFFFFF")
        if changed_after_open:
            return SimpleNamespace(close=lambda: calls.append("close"))
        raise PermissionError(13, "udev race")
    monkeypatch.setattr(install, "_ProtocolClient", open_client)
    with pytest.raises(install.FirmwareInstallError, match="identity changed|different device"):
        io._open_runtime_client(device, timeout=1)
    assert calls == (["open", "close"] if changed_after_open else ["open"])


def test_runtime_open_does_not_retry_unrelated_io_errors(runtime_io, monkeypatch):
    io, device, elapsed = runtime_io
    monkeypatch.setattr(install, "_ProtocolClient", lambda _: (_ for _ in ()).throw(OSError(5, "USB I/O error")))
    with pytest.raises(OSError, match="USB I/O error"):
        io._open_runtime_client(device, timeout=1)
    assert elapsed[0] == 0


def test_rollback_records_full_flash_verification_even_if_runtime_snapshot_fails(environment):
    class IO(FakeIO):
        def snapshot(self, device, *, save_dirty, expected_info=None):
            if not save_dirty:
                raise PermissionError(13, "runtime port not accessible")
            return super().snapshot(device, save_dirty=save_dirty, expected_info=expected_info)
    io = IO(failure="load_firmware")
    reports = []
    result = run_job(environment, io, reports.append)
    assert result["status"] == "manual_recovery"
    assert result["rollback_flash_verified"] is True
    assert result["rollback_verified"] is False
    assert result["flash_may_be_modified"] is True
    assert bytes(io.flash) == io.original
    verified = next(state for state in reports if state.get("rollback_flash_verified"))
    assert verified["phase"] == "rolling_back"


def test_readonly_snapshot_rejects_unsaved_edits_that_appear_during_read(monkeypatch):
    commands = []
    def request(command, response, **kwargs):
        commands.append(command)
        if command == "GET_DEVICE_INFO":
            return {"fw": "0.6.5-native"}
        if command == "GET_DIRTY":
            return {"patches": [] if commands.count(command) == 1 else [{"bank": 1, "slot": 1}]}
        if command == "LIST_PROFILES":
            return {"profiles": [], "active": ""}
        pytest.fail("Unexpected device command: " + command)
    io = object.__new__(install.LinuxInstallIO)
    monkeypatch.setattr(io, "_open_runtime_client", lambda _: SimpleNamespace(request=request, close=lambda: None))
    with pytest.raises(install.FirmwareInstallError, match="Unsaved changes appeared"):
        io.snapshot(install.PinnedDevice("/sys/pinned", "1234567890ABCDEF"), save_dirty=False)
    assert commands == ["GET_DEVICE_INFO", "GET_DIRTY", "LIST_PROFILES", "LIST_PROFILES", "GET_DIRTY"]
