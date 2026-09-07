"""Native firmware installation on the hub's physically pinned RP2040.

The caller must stop and join its UpstreamLink before calling ``run`` and
restart a fresh link afterwards. This synchronous worker belongs in a thread;
closing a desktop connection must not cancel flash verification or rollback.
ROM flashing is not an A/B update: an interrupted job retains a durable full
flash backup and journal, and may require physical BOOTSEL recovery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import time
from typing import Callable, Protocol

from .update_package import (
    FLASH_BASE, FLASH_BYTES, STORAGE_BYTES, STORAGE_OFFSET,
    build_native_storage, extract_circuitpython_config, validate_update_package,
)


class FirmwareInstallError(RuntimeError):
    pass


class FirmwareResponseTimeout(FirmwareInstallError):
    pass


NATIVE_MESSAGES = frozenset({
    "cc", "pc", "note_on", "note_off", "delay", "program_change_bank", "cc_toggle",
    "captain_patch", "captain_bank_step", "captain_preview_step", "captain_preview_commit",
    "captain_preview_cancel", "captain_setlist_step", "kemper_rig", "kemper_step_rig",
    "kemper_effect_toggle", "kemper_fixed_toggle", "kemper_tuner", "kemper_tap_tempo",
    "kemper_set_tempo", "kemper_morph", "kemper_morph_trigger", "kemper_wah", "kemper_volume",
    "kemper_looper", "kemper_rotary", "kemper_query_state",
})
NATIVE_SWITCHES = frozenset({"1", "2", "3", "4", "up", "A", "B", "C", "D", "down"})
NATIVE_MODES = frozenset({"tap", "latched", "momentary", "long_press_alt", "double_tap"})
NATIVE_ACTIONS = frozenset({"press", "release", "toggle_on", "toggle_off", "long_press", "double_tap"})
NATIVE_COMMAND_LIMIT = 128
NATIVE_NAVIGATION_LIMIT = 128


def _require_native_compatible(snapshot: dict) -> None:
    """Reject known behavior losses before switching a CP device to BOOTSEL.

    Unknown non-behavior settings remain untouched in the raw config tree.
    Limits/types mirror the native runtime, with source-parity regression tests.
    The production C storage builder separately checks JSON/token/flash limits.
    """
    for profile_id, profile in snapshot["profiles"].items():
        def fail(location, detail):
            raise FirmwareInstallError(f"Profile {profile_id}, {location}: {detail}; CircuitPython was left unchanged")
        if profile["metadata"].get("kind") not in ("kemper_player", "generic_midi", "unknown"):
            fail("plugin", "this plugin is not supported by native firmware")
        device, patches = profile["device"], profile["patches"]
        kemper_enabled = isinstance(device.get("kemper"), dict)
        used_types = set()
        def message(value, location):
            if not isinstance(value, dict) or value.get("type") not in NATIVE_MESSAGES:
                fail(location, "unsupported MIDI message type")
            kind = value["type"]
            used_types.add(kind)
            if kind.startswith("kemper_") and not kemper_enabled:
                fail(location, "Kemper messages require the profile's Kemper configuration")
            def number(key, low, high):
                if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
                    fail(location, f"{key} exceeds native limits {low}..{high}")
            def enum(key, choices):
                if key in value and value[key] not in choices:
                    fail(location, f"unsupported {key}")
            number("channel", 1, 16)
            if kind in ("cc", "cc_toggle"):
                number("cc", 0, 127)
                if kind == "cc": number("value", 0, 127)
                else:
                    enum("state", ("on", "off"))
                    number("on_value", 0, 127); number("off_value", 0, 127)
            elif kind in ("pc", "program_change_bank"):
                number("program", 0, 127)
                if kind == "program_change_bank":
                    number("msb", 0, 127); number("lsb", 0, 127)
            elif kind in ("note_on", "note_off"):
                number("note", 0, 127); number("velocity", 0, 127)
            elif kind == "delay": number("ms", 0, 60000)
            elif kind in ("captain_patch", "kemper_rig"):
                number("bank", 1, 99 if kind == "captain_patch" else 25)
                number("slot" if kind == "captain_patch" else "rig", 1, 10 if kind == "captain_patch" else 5)
            elif kind in ("captain_bank_step", "captain_preview_step", "captain_setlist_step"):
                number("delta", -32768, 32767); enum("scope", ("patch", "bank"))
            elif kind == "kemper_effect_toggle":
                enum("slot", ("A", "B", "C", "D", "X", "Mod", "Delay", "Reverb")); enum("value", ("on", "off"))
            elif kind == "kemper_fixed_toggle":
                enum("effect", ("Compressor", "Noise Gate", "Pure Booster", "Wah", "Transpose")); enum("value", ("on", "off"))
            elif kind == "kemper_looper": enum("action", ("rec_play", "stop_erase", "trigger", "reverse", "half_speed"))
            elif kind == "kemper_step_rig": enum("direction", ("prev", "next"))
            elif kind == "kemper_set_tempo": number("bpm", 40, 250)
            elif kind in ("kemper_tuner", "kemper_morph_trigger"): enum("state", ("on", "off"))
            elif kind == "kemper_rotary": enum("value", ("slow", "fast"))
            elif kind in ("kemper_wah", "kemper_volume", "kemper_morph"): number("value", 0, 127)
        def messages(values, location):
            if not isinstance(values, list) or len(values) > NATIVE_COMMAND_LIMIT:
                fail(location, "action exceeds the native 128-message limit")
            for item in values:
                message(item, location)
            return len(values)
        def action(value, location):
            if not isinstance(value, dict): fail(location, "invalid action object")
            return messages(value.get("messages", []), location)
        def expression(document, location, *, override=False):
            rows = document.get("expression", [])
            if not isinstance(rows, list): fail(location, "invalid expression configuration")
            jacks = set()
            for row in rows:
                if not isinstance(row, dict) or type(row.get("jack")) is not int or row["jack"] not in (1, 2) or row["jack"] in jacks:
                    fail(location, "invalid or duplicate expression jack")
                jacks.add(row["jack"])
                if row.get("curve", "linear") not in ("linear", "exp", "log"):
                    fail(location, "unsupported expression curve")
                if "message" in row:
                    message(row["message"], location)
                    if (override or row.get("enabled")) and row["message"]["type"] not in ("cc", "cc_toggle", "kemper_wah", "kemper_volume", "kemper_morph"):
                        fail(location, "unsupported continuous expression message")
        long_actions = device.get("long_press_actions", {})
        if not isinstance(long_actions, dict): fail("long press", "invalid action map")
        for switch, values in long_actions.items():
            if switch not in NATIVE_SWITCHES: fail("long press", "unsupported switch")
            messages(values, "long press " + switch)
        expression(device, "expression")
        maximum_enter = maximum_exit = 0
        for coordinate, patch in patches.items():
            if "on_enter" in patch: maximum_enter = max(maximum_enter, action(patch["on_enter"], coordinate + " on_enter"))
            if "on_exit" in patch: maximum_exit = max(maximum_exit, action(patch["on_exit"], coordinate + " on_exit"))
            bindings = patch.get("bindings", [])
            if not isinstance(bindings, list) or len(bindings) > len(NATIVE_SWITCHES):
                fail(coordinate, "unsupported binding count")
            switches = set()
            for binding in bindings:
                if not isinstance(binding, dict) or binding.get("switch") not in NATIVE_SWITCHES or binding["switch"] in switches:
                    fail(coordinate, "invalid or duplicate switch binding")
                switches.add(binding["switch"])
                if binding.get("mode", "tap") not in NATIVE_MODES: fail(coordinate, "unsupported binding mode")
                actions = binding.get("actions", {})
                if not isinstance(actions, dict) or set(actions) - NATIVE_ACTIONS:
                    fail(coordinate, "unsupported binding action")
                for name, value in actions.items(): action(value, coordinate + " " + binding["switch"] + " " + name)
            expression(patch, coordinate + " expression", override=True)
        if maximum_enter + maximum_exit > NATIVE_COMMAND_LIMIT:
            fail("patch transition", "combined on_exit/on_enter exceeds 128 messages")
        if used_types & {"captain_bank_step", "captain_preview_step"} and len(patches) > NATIVE_NAVIGATION_LIMIT:
            fail("navigation", "native navigation supports at most 128 patches")
        if "captain_setlist_step" in used_types:
            setlist = device.get("setlist", {})
            if not isinstance(setlist, dict) or not isinstance(setlist.get("items", []), list) or len(setlist.get("items", [])) > NATIVE_NAVIGATION_LIMIT:
                fail("setlist", "native navigation supports at most 128 setlist entries")


@dataclass(frozen=True)
class PinnedDevice:
    usb_path: str
    serial: str
    data_interface: str = "02"


@dataclass(frozen=True)
class BootDevice:
    device: PinnedDevice
    unique_id: str
    flash_bytes: int
    rom_serial: str


class InstallIO(Protocol):
    def pin(self, serial_port: str) -> PinnedDevice: ...
    def snapshot(self, device: PinnedDevice, *, save_dirty: bool, expected_info: dict | None = None) -> dict: ...
    def enter_bootloader(self, device: PinnedDevice, family: str) -> BootDevice: ...
    def recover_bootloader(self, device: PinnedDevice) -> BootDevice: ...
    def save_flash(self, target: BootDevice, destination: Path) -> None: ...
    def verify(self, target: BootDevice, image: Path, address: int | None = None) -> None: ...
    def load(self, target: BootDevice, image: Path, address: int | None = None) -> None: ...
    def reboot(self, target: BootDevice) -> None: ...


def _family(info: dict) -> str:
    return "native" if info.get("native_experimental") is True or "-native" in str(info.get("fw", "")) else "circuitpython"


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, contents: bytes) -> None:
    with path.open("xb") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(path.parent)


def _require_snapshot(snapshot: dict) -> None:
    info = snapshot.get("info")
    profiles = snapshot.get("profiles")
    if not isinstance(info, dict) or not info.get("fw") or not isinstance(profiles, dict):
        raise FirmwareInstallError("Incomplete device/configuration snapshot")
    if len(profiles) > 32 or snapshot.get("active", "") not in ("", *profiles):
        raise FirmwareInstallError("Invalid active profile or unsupported profile count")
    for profile in profiles.values():
        if not isinstance(profile, dict) or any(not isinstance(profile.get(key), dict)
                for key in ("metadata", "device", "patches", "midi_learn")):
            raise FirmwareInstallError("Incomplete profile snapshot; no firmware was written")


def _compare_snapshot(before: dict, after: dict, version: str) -> None:
    _require_snapshot(after)
    if after["info"].get("fw") != version:
        raise FirmwareInstallError("The device did not boot the expected firmware version")
    if before["active"] != after["active"] or before["profiles"] != after["profiles"]:
        raise FirmwareInstallError("Profile, settings, patch or MIDI Learn readback differs from the backup")


def read_prewrite_recovery(installation_dir: Path) -> tuple[dict, dict]:
    """Inspect original evidence without changing the job or opening a device."""
    installation_dir = Path(installation_dir)
    if (not installation_dir.is_absolute() or installation_dir.is_symlink()
            or installation_dir.resolve(strict=True) != installation_dir):
        raise FirmwareInstallError("Recovery requires the original installation directory")
    values = []
    for name, limit in (("journal.json", 65536), ("configuration-before.json", 16 * 1024 * 1024)):
        path = installation_dir / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise FirmwareInstallError("Recovery evidence is missing or invalid: " + name)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise FirmwareInstallError("Recovery evidence is not a JSON object: " + name)
        values.append(value)
    journal, before = values
    if journal.get("flash_may_be_modified") is not False:
        raise FirmwareInstallError("This update may have written flash; a verified full-image recovery is required")
    _require_snapshot(before)
    if journal.get("source_version") != before["info"]["fw"]:
        raise FirmwareInstallError("Original firmware identity is missing or inconsistent in the recovery evidence")
    try:
        device = PinnedDevice(**journal["device"])
    except (KeyError, TypeError) as error:
        raise FirmwareInstallError("Original USB identity is missing from the recovery evidence") from error
    if (not isinstance(device.usb_path, str) or not device.usb_path.startswith("/sys/")
            or not isinstance(device.serial, str) or not re.fullmatch(r"[0-9A-F]{16}", device.serial)
            or device.data_interface != "02"):
        raise FirmwareInstallError("Original USB identity is invalid in the recovery evidence")
    return journal, before


def verify_prewrite_recovery(installation_dir: Path, serial_port: str, *,
                            expected_device: PinnedDevice, io: InstallIO | None = None) -> dict:
    """Verify a returned Captain, without rebooting or writing its firmware/config.

    The service must release its serial owner first. Original evidence remains
    untouched; each successful check publishes a new, durable recovery record.
    """
    journal, before = read_prewrite_recovery(installation_dir)
    device = PinnedDevice(**journal["device"])
    if device != expected_device:
        raise FirmwareInstallError("Recovery device does not match the hub's opened USB identity")
    io = io or LinuxInstallIO()
    if io.pin(serial_port) != device:
        raise FirmwareInstallError("The connected Captain changed before recovery verification")
    after = io.snapshot(device, save_dirty=False, expected_info=before["info"])
    _compare_snapshot(before, after, journal["source_version"])
    if _family(before["info"]) != _family(after["info"]) or io.pin(serial_port) != device:
        raise FirmwareInstallError("The connected Captain changed during recovery verification")
    current_journal, current_before = read_prewrite_recovery(installation_dir)
    if current_journal != journal or current_before != before:
        raise FirmwareInstallError("The original recovery evidence changed during verification")
    record = {"verified_at": time.time(), "device": journal["device"],
              "flash_may_be_modified": False, "original_version_verified": True,
              "configuration_snapshot_matched": True, "after": after,
              "journal_sha256": hashlib.sha256((installation_dir / "journal.json").read_bytes()).hexdigest(),
              "configuration_before_sha256": hashlib.sha256((installation_dir / "configuration-before.json").read_bytes()).hexdigest()}
    record_path = installation_dir.parent / "recovery-record.json"
    if record_path.exists():
        # A verified check may have outlived a failed final status fsync. Keep
        # its evidence and write a new record when the user retries the check.
        record_path = record_path.with_name("recovery-record-" + secrets.token_hex(8) + ".json")
    _write_new(record_path, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return {"recovery_verified": True, "recovery_record": str(record_path)}


class FirmwareInstaller:
    """One durable transaction. ``io`` is injectable for fault tests."""

    def __init__(self, *, converter: Path, io: InstallIO | None = None):
        self.converter = converter
        self.io = io

    def run(self, package_path: Path, expected_info: dict, serial_port: str,
            job_dir: Path, progress: Callable[[dict], None], *,
            expected_device: PinnedDevice | None = None) -> dict:
        job_dir = Path(job_dir)
        if not job_dir.is_absolute() or job_dir.parent.resolve(strict=True) != job_dir.parent:
            raise FirmwareInstallError("Update job parent must be an existing absolute directory without symlinks")
        job_dir.mkdir(mode=0o700)
        _sync_directory(job_dir.parent)
        state = {"status": "running", "phase": "preflight", "job_dir": str(job_dir),
                 "flash_may_be_modified": False, "started_at": time.time()}

        def report(phase: str, *, required: bool = True, **values) -> None:
            state.update(values, phase=phase, updated_at=time.time())
            temporary = job_dir / (".journal-" + secrets.token_hex(8))
            try:
                _write_new(temporary, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())
                os.replace(temporary, job_dir / "journal.json")
                _sync_directory(job_dir)
            except Exception as journal_error:
                if required:
                    raise
                # The pre-write journal and full backup are already durable.
                # A newly full/broken state disk must not prevent restoring
                # the verified image to a partially programmed Captain.
                state["journal_error"] = str(journal_error)
            try:
                progress(dict(state))
            except Exception:
                # A disconnected UI must never abort a flash or rollback.
                pass

        target = None
        device = None
        before = None
        backup = job_dir / "full-flash-before.bin"
        verified_backup = False
        entered_bootloader = False
        writes_started = False
        io = self.io
        report("preflight")
        try:
            package = validate_update_package(Path(package_path))
            io = io or LinuxInstallIO()
            if isinstance(io, LinuxInstallIO) and expected_device is None:
                raise FirmwareInstallError("The hub did not preserve the connected Captain's physical USB identity")
            device = io.pin(serial_port)
            if expected_device is not None and device != expected_device:
                raise FirmwareInstallError("The selected USB device changed before the hub released its connection")
            report("reading_configuration", device=asdict(device),
                   release=package.manifest["release"], target_version=package.manifest["firmware_version"])
            before = io.snapshot(device, save_dirty=True, expected_info=expected_info)
            _require_snapshot(before)
            if before["info"].get("fw") != expected_info.get("fw") or _family(before["info"]) != _family(expected_info):
                raise FirmwareInstallError("The connected firmware changed before the update began")
            if _family(before["info"]) == "circuitpython":
                _require_native_compatible(before)
            _write_new(job_dir / "configuration-before.json", (json.dumps(before, indent=2, sort_keys=True) + "\n").encode())
            firmware = job_dir / "firmware.uf2"
            _write_new(firmware, package.firmware_uf2)
            report("entering_bootloader", source_version=before["info"]["fw"])
            entered_bootloader = True
            target = io.enter_bootloader(device, _family(before["info"]))
            if target.flash_bytes != FLASH_BYTES or target.unique_id.upper() != device.serial.upper():
                raise FirmwareInstallError("ROM flash identity/geometry does not match the pinned Captain")
            report("backing_up_flash", flash_unique_id=target.unique_id, flash_bytes=target.flash_bytes,
                   rom_usb_serial=target.rom_serial)
            io.save_flash(target, backup)
            if backup.is_symlink() or backup.stat().st_size != FLASH_BYTES:
                raise FirmwareInstallError("Full flash backup is missing or incomplete")
            with backup.open("r+b") as saved:
                os.fsync(saved.fileno())
                raw = saved.read()
            _sync_directory(job_dir)
            io.verify(target, backup, FLASH_BASE)
            digest = hashlib.sha256(raw).hexdigest()
            verified_backup = True
            report("backup_verified", backup_path=str(backup), backup_sha256=digest)
            storage = job_dir / "native-storage.bin"
            if _family(before["info"]) == "circuitpython":
                report("preparing_native_storage")
                config = job_dir / "config"
                extracted = extract_circuitpython_config(raw, config)
                # The trusted host builder uses the production littlefs and
                # native config parser, and verifies remount/readback before
                # publishing a fresh 512 KiB image. No on-device formatting.
                storage_bytes = build_native_storage(config, storage, builder=self.converter)
                if len(storage_bytes) != STORAGE_BYTES:
                    raise FirmwareInstallError("Invalid native storage image size")
                report("storage_prepared", configuration_files=len(extracted),
                       storage_sha256=hashlib.sha256(storage_bytes).hexdigest())
            else:
                # Native upgrades preserve the complete filesystem, including
                # unknown future settings. Verify it after loading the UF2.
                _write_new(storage, raw[STORAGE_OFFSET:])
            writes_started = True
            report("flashing_firmware", flash_may_be_modified=True)
            io.load(target, firmware)
            io.verify(target, firmware)
            if _family(before["info"]) == "circuitpython":
                report("flashing_configuration")
                io.load(target, storage, FLASH_BASE + STORAGE_OFFSET)
            io.verify(target, storage, FLASH_BASE + STORAGE_OFFSET)
            report("rebooting")
            io.reboot(target)
            report("verifying_configuration")
            after = io.snapshot(device, save_dirty=False)
            _compare_snapshot(before, after, package.manifest["firmware_version"])
            if _family(after["info"]) != "native":
                raise FirmwareInstallError("The device did not boot native firmware")
            _write_new(job_dir / "configuration-after.json", (json.dumps(after, indent=2, sort_keys=True) + "\n").encode())
            report("complete", status="complete", configuration_verified=True)
        except Exception as error:
            original_error = str(error)
            if writes_started and verified_backup and io is not None and device is not None and before is not None:
                try:
                    report("rolling_back", required=False, error=original_error)
                    # Validate the durable copy again immediately before it is
                    # used as a recovery image, never a caller-supplied path.
                    if backup.stat().st_size != FLASH_BYTES or hashlib.sha256(backup.read_bytes()).hexdigest() != state["backup_sha256"]:
                        raise FirmwareInstallError("The full flash backup no longer matches its verified hash")
                    recovery = io.recover_bootloader(device)
                    if recovery.unique_id != target.unique_id or recovery.flash_bytes != FLASH_BYTES or recovery.rom_serial != target.rom_serial:
                        raise FirmwareInstallError("Rollback target identity changed")
                    io.load(recovery, backup, FLASH_BASE)
                    io.verify(recovery, backup, FLASH_BASE)
                    report("rolling_back", required=False, rollback_flash_verified=True)
                    io.reboot(recovery)
                    restored = io.snapshot(device, save_dirty=False)
                    _compare_snapshot(before, restored, before["info"]["fw"])
                    report("rolled_back", required=False, status="rolled_back", error=original_error,
                           rollback_verified=True)
                except Exception as recovery_error:
                    report("manual_recovery", required=False, status="manual_recovery", error=original_error,
                           recovery_error=str(recovery_error), rollback_verified=False)
            else:
                recovery_error = None
                if entered_bootloader and io is not None and device is not None:
                    try:
                        target = io.recover_bootloader(device)
                        io.reboot(target)
                    except Exception as resume_error:
                        recovery_error = str(resume_error)
                status = "manual_recovery" if recovery_error else "failed"
                report(status, required=False, status=status, error=original_error,
                       recovery_error=recovery_error, flash_may_be_modified=False)
        return dict(state)


class _ProtocolClient:
    def __init__(self, port: str):
        import serial
        self.port = serial.Serial(port, 115200, timeout=0.15, write_timeout=3, exclusive=True)
        self._pending = bytearray()
        try:
            self.port.reset_input_buffer()
            self.request("PING", "ACK", startup=True)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.port.close()

    def request(self, command: str, response: str, *, startup: bool = False, **fields) -> dict:
        correlation = "update-" + secrets.token_hex(8)
        request = {"type": command, "id": correlation, **fields}
        wire = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        if self.port.write(wire) != len(wire):
            raise FirmwareInstallError("Incomplete serial write")
        self.port.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # A single read may contain the correlated reply followed by part
            # of an unsolicited event. Preserve that tail across requests;
            # dropping it would turn the next valid frame into malformed JSON.
            while b"\n" in self._pending:
                line, _, remainder = self._pending.partition(b"\n")
                self._pending = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    reply = json.loads(line)
                except (ValueError, UnicodeError):
                    if startup:
                        continue
                    raise FirmwareInstallError(
                        f"Malformed device response during update ({command}, {len(line)} bytes): {bytes(line[:128])!r}")
                if not isinstance(reply, dict) or reply.get("id") != correlation:
                    continue
                if reply.get("type") != response:
                    raise FirmwareInstallError(f"{command} failed: {reply.get('error', reply.get('type'))}")
                return reply
            self._pending.extend(self.port.read(4096))
            if len(self._pending) > 65536:
                raise FirmwareInstallError("Device response exceeds update protocol limits")
        raise FirmwareResponseTimeout(f"No correlated response to {command}")


class LinuxInstallIO:
    """Targeted sysfs/CDC/picotool operations; no shell and no sudo."""

    def __init__(self, *, picotool: Path | None = None, sysfs: Path = Path("/sys"),
                 dev: Path = Path("/dev")):
        tool = str(picotool) if picotool is not None else shutil.which("picotool")
        if not tool:
            raise FirmwareInstallError("Native updates require the installed picotool helper")
        self.picotool = str(Path(tool).resolve(strict=True))
        self.sysfs, self.dev = sysfs, dev

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="ascii").strip()

    def _usb_parent(self, tty: str) -> tuple[Path, str]:
        node = (self.sysfs / "class/tty" / Path(tty).name / "device").resolve(strict=True)
        interface = None
        for parent in (node, *node.parents):
            if (parent / "bInterfaceNumber").is_file():
                interface = self._read(parent / "bInterfaceNumber").lower()
            if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
                if interface is None:
                    raise FirmwareInstallError("The selected port is not an identifiable USB CDC interface")
                return parent, interface
        raise FirmwareInstallError("Cannot identify the selected Captain USB parent")

    def pin(self, serial_port: str) -> PinnedDevice:
        parent, interface = self._usb_parent(serial_port)
        if (self._read(parent / "idVendor").lower(), self._read(parent / "idProduct").lower(), interface) != ("239a", "80f4", "02"):
            raise FirmwareInstallError("Native updates require the selected MIDI Captain data interface")
        serial = self._read(parent / "serial").upper()
        if not re.fullmatch(r"[0-9A-F]{16}", serial):
            raise FirmwareInstallError("Captain USB serial does not provide a usable flash identity")
        return PinnedDevice(str(parent), serial, interface)

    def _runtime_port(self, device: PinnedDevice, interface: str = "02", timeout: float = 20) -> str:
        deadline = time.monotonic() + timeout
        permissions_pending = False
        while True:
            for tty in (self.sysfs / "class/tty").glob("ttyACM*"):
                try:
                    parent, number = self._usb_parent(tty.name)
                    if str(parent) != device.usb_path or number != interface:
                        continue
                    if self._read(parent / "serial").upper() != device.serial:
                        raise FirmwareInstallError("A different device occupies the selected USB port")
                    if (self._read(parent / "idVendor").lower(), self._read(parent / "idProduct").lower()) != ("239a", "80f4"):
                        raise FirmwareInstallError("The selected USB device identity changed")
                    port = str(self.dev / tty.name)
                    # Sysfs appears before udev has assigned the tty owner and
                    # group. In particular a fresh native boot can expose
                    # ttyACM1 while opening it still fails with EACCES.
                    if not os.access(port, os.R_OK | os.W_OK):
                        permissions_pending = True
                        continue
                    return port
                except (FileNotFoundError, OSError):
                    continue
            if time.monotonic() >= deadline:
                raise FirmwareInstallError("The pinned Captain data port did not become accessible" if permissions_pending else
                                           "The pinned Captain data port did not return")
            time.sleep(0.15)

    def _open_runtime_client(self, device: PinnedDevice, timeout: float = 20) -> _ProtocolClient:
        deadline = time.monotonic() + timeout
        while True:
            port = self._runtime_port(device, timeout=max(0, deadline - time.monotonic()))
            client = None
            try:
                client = _ProtocolClient(port)
                # Revalidate the physical USB identity after opening as well:
                # a recycled tty name must never receive reboot commands.
                if self.pin(port) != device:
                    raise FirmwareInstallError("The Captain USB identity changed while opening its data port")
                return client
            except OSError as error:
                if client is not None:
                    client.close()
                if error.errno not in (errno.EACCES, errno.ENOENT):
                    raise
                if time.monotonic() >= deadline:
                    raise FirmwareInstallError("The pinned Captain data port did not become accessible") from error
                # access() and open() are not atomic. Retry only these two
                # transient enumeration races, with fresh identity checks.
                time.sleep(0.15)
            except Exception:
                if client is not None:
                    client.close()
                raise

    def snapshot(self, device: PinnedDevice, *, save_dirty: bool, expected_info: dict | None = None) -> dict:
        client = self._open_runtime_client(device)
        try:
            info = client.request("GET_DEVICE_INFO", "DEVICE_INFO")
            if expected_info is not None and (info.get("fw") != expected_info.get("fw") or _family(info) != _family(expected_info)):
                raise FirmwareInstallError("The connected firmware changed before saving pending edits")
            dirty = client.request("GET_DIRTY", "DIRTY").get("patches")
            if not isinstance(dirty, list):
                raise FirmwareInstallError("Cannot verify unsaved device changes")
            if dirty:
                # Preserve the saved/unsaved distinction. Even SAVE_NOW is a
                # flash write, so a migration preflight never commits edits.
                raise FirmwareInstallError("Save edits before updating firmware" if save_dirty else
                                           "Unexpected unsaved changes after the update")
            listing = client.request("LIST_PROFILES", "PROFILE_LIST")
            entries = listing.get("profiles")
            if not isinstance(entries, list) or len(entries) > 32:
                raise FirmwareInstallError("Unsupported or incomplete profile inventory")
            snapshot = {"info": info, "active": listing.get("active", ""), "profiles": {}}
            for entry in entries:
                profile = entry.get("id")
                if not isinstance(profile, str) or not profile or profile in snapshot["profiles"]:
                    raise FirmwareInstallError("Invalid or duplicate profile identifier")
                device_reply = client.request("GET_GLOBAL", "GLOBAL", profile=profile)
                patches_reply = client.request("LIST_PATCHES", "PATCH_LIST", profile=profile)
                learn_reply = client.request("GET_MIDI_LEARN", "MIDI_LEARN", profile=profile)
                for response in (device_reply, patches_reply, learn_reply):
                    if response.get("profile") != profile:
                        raise FirmwareInstallError("Firmware cannot confirm cross-profile backup identity")
                patches = patches_reply.get("patches")
                if not isinstance(patches, list) or len(patches) > 256:
                    raise FirmwareInstallError("Unsupported or incomplete patch inventory")
                row = {"metadata": {key: entry.get(key) for key in ("id", "name", "kind", "color")},
                       "device": device_reply.get("device"), "midi_learn": learn_reply.get("table"), "patches": {}}
                for patch in patches:
                    bank, slot = patch.get("bank"), patch.get("slot")
                    if type(bank) is not int or type(slot) is not int or not 1 <= bank <= 99 or not 1 <= slot <= 10:
                        raise FirmwareInstallError("Patch coordinates exceed native firmware limits")
                    key = f"{bank:02}/{slot:02}"
                    if key in row["patches"]:
                        raise FirmwareInstallError("Duplicate patch in device inventory")
                    reply = client.request("GET_PATCH", "PATCH", profile=profile, bank=bank, slot=slot)
                    if reply.get("profile") != profile or (reply.get("bank"), reply.get("slot")) != (bank, slot) or not isinstance(reply.get("patch"), dict):
                        raise FirmwareInstallError("Patch backup identity or content is invalid")
                    row["patches"][key] = reply["patch"]
                snapshot["profiles"][profile] = row
            ending = client.request("LIST_PROFILES", "PROFILE_LIST")
            if ending.get("profiles") != entries or ending.get("active") != snapshot["active"]:
                raise FirmwareInstallError("Profiles changed while taking the update snapshot")
            if client.request("GET_DIRTY", "DIRTY").get("patches") != []:
                raise FirmwareInstallError("Unsaved changes appeared while checking the configuration")
            _require_snapshot(snapshot)
            return snapshot
        finally:
            client.close()

    def _command(self, arguments: list[str], *, timeout: float = 180) -> str:
        completed = subprocess.run([self.picotool, *arguments], check=False,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, timeout=timeout)
        if completed.returncode:
            raise FirmwareInstallError("picotool failed: " + (completed.stderr or completed.stdout).strip()[-2000:])
        return completed.stdout

    def _boot_device(self, device: PinnedDevice, timeout: float = 15) -> tuple[BootDevice, list[str]]:
        deadline = time.monotonic() + timeout
        path = Path(device.usb_path)
        while True:
            try:
                if (self._read(path / "idVendor").lower(), self._read(path / "idProduct").lower()) == ("2e8a", "0003"):
                    # RP2040 ROM USB serial is a non-unique 12-hex chip/revision
                    # string; runtime serial is the external NOR flash UID.
                    # picotool deliberately uses the FLASH UID for --ser on
                    # RP2040, not the USB descriptor (picotool 2.1.1,
                    # picoboot_connection/picoboot_connection.c:184-192).
                    # Keep the ROM descriptor as an additional continuity
                    # check and independently verify the reported flash UID.
                    rom_serial = self._read(path / "serial")
                    if not re.fullmatch(r"[0-9A-Fa-f]{12}", rom_serial):
                        raise FirmwareInstallError("The pinned BOOTSEL device has an invalid ROM USB serial")
                    selection = ["--bus", str(int(self._read(path / "busnum"))),
                                 "--address", str(int(self._read(path / "devnum"))),
                                 "--ser", device.serial]
                    text = self._command(["info", "-a", *selection], timeout=20)
                    unique = re.search(r"(?im)^\s*(?:flash\s+)?(?:unique\s+)?(?:device\s+)?id\s*:\s*(?:0x)?([0-9a-f]{16})\b", text)
                    size = re.search(r"(?im)^\s*flash\s+size\s*:\s*(\d+)\s*([KM])(?:i?B)?\b", text)
                    if not unique or not size:
                        raise FirmwareInstallError("picotool did not report an unambiguous flash ID and capacity")
                    capacity = int(size[1]) * (1024 if size[2].upper() == "K" else 1024 * 1024)
                    target = BootDevice(device, unique[1].upper(), capacity, rom_serial)
                    if target.unique_id != device.serial or capacity != FLASH_BYTES:
                        raise FirmwareInstallError("BOOTSEL identity/flash capacity does not match the pinned Captain")
                    return target, selection
            except (FileNotFoundError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise FirmwareInstallError("The pinned Captain did not enumerate in BOOTSEL")
            time.sleep(0.15)

    def enter_bootloader(self, device: PinnedDevice, family: str) -> BootDevice:
        if family == "native":
            client = self._open_runtime_client(device)
            try:
                info = client.request("GET_DEVICE_INFO", "DEVICE_INFO")
                if _family(info) != "native" or "bootloader" not in info.get("reboot_modes", []):
                    raise FirmwareInstallError("This native firmware cannot enter BOOTSEL remotely")
                try:
                    client.request("REBOOT", "ACK", mode="bootloader")
                except (OSError, FirmwareResponseTimeout):
                    # USB can disappear before the queued ACK reaches the Pi.
                    # Success still requires the same physical device, flash
                    # UID and capacity to appear in BOOTSEL below. A correlated
                    # refusal or malformed response must remain an error.
                    pass
            finally:
                client.close()
        else:
            import serial
            # Touch only the console paired with the pinned data interface.
            # Never broadcast control bytes to other MIDI/serial devices.
            console = self._runtime_port(device, "00")
            with serial.Serial(console, 1200, timeout=0.1, write_timeout=2, exclusive=True) as port:
                port.dtr = False
                time.sleep(0.25)
            try:
                return self._boot_device(device, timeout=3)[0]
            except FirmwareInstallError:
                console = self._runtime_port(device, "00", timeout=2)
                with serial.Serial(console, 115200, timeout=0.1, write_timeout=2, exclusive=True) as port:
                    port.dtr = True
                    port.write(b"\x03\x03")
                    time.sleep(0.4)
                    port.write(b"\r\nimport microcontroller\r\nmicrocontroller.on_next_reset(getattr(microcontroller.RunMode,'UF2',microcontroller.RunMode.BOOTLOADER))\r\nmicrocontroller.reset()\r\n")
                    port.flush()
        return self._boot_device(device)[0]

    def recover_bootloader(self, device: PinnedDevice) -> BootDevice:
        try:
            return self._boot_device(device, timeout=0.3)[0]
        except FirmwareInstallError:
            client = self._open_runtime_client(device, timeout=10)
            try:
                info = client.request("GET_DEVICE_INFO", "DEVICE_INFO")
            finally:
                client.close()
            return self.enter_bootloader(device, _family(info))

    def _target_arguments(self, target: BootDevice) -> list[str]:
        current, selection = self._boot_device(target.device, timeout=1)
        if (current.unique_id != target.unique_id or current.flash_bytes != target.flash_bytes or
                current.rom_serial != target.rom_serial):
            raise FirmwareInstallError("The flash target changed during the update")
        return selection

    def save_flash(self, target: BootDevice, destination: Path) -> None:
        if target.flash_bytes != FLASH_BYTES or FLASH_BYTES != 8 * 1024 * 1024:
            raise FirmwareInstallError("A full flash backup requires the known 8 MiB Captain geometry")
        chunk_bytes = 1024 * 1024
        deadline = time.monotonic() + 600

        def command_timeout() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FirmwareInstallError("The full flash backup exceeded its 600 second time limit")
            return min(180, remaining)

        try:
            output = destination.open("xb")
        except FileExistsError as error:
            raise FirmwareInstallError("Refusing to overwrite an existing full flash backup") from error
        with output:
            # picotool 2.1.1 reads raw BIN saves in 256-byte pages and scans its
            # growing flash cache for every page (main.cpp:1756, 4105). Fresh
            # processes bound that quadratic work to 1 MiB per cache. -r uses
            # an exclusive end address and writes BIN offsets relative to start.
            parts = destination.with_name(destination.name + ".parts")
            parts.mkdir(mode=0o700)
            total = 0
            for offset in range(0, FLASH_BYTES, chunk_bytes):
                part = parts / f"{offset:08x}.bin"
                try:
                    command_timeout()
                    selection = self._target_arguments(target)
                    # Only the trusted tool may overwrite this newly reserved
                    # file in our private directory. Never reuse old artifacts.
                    with part.open("xb"):
                        pass
                    self._command(["save", "-r", hex(FLASH_BASE + offset),
                                   hex(FLASH_BASE + offset + chunk_bytes), str(part), *selection],
                                  timeout=command_timeout())
                    command_timeout()
                    metadata = part.lstat()
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != chunk_bytes:
                        raise FirmwareInstallError("Backup fragment is not a regular file of exactly 1 MiB")
                    # Do not follow a replaced symlink or block on a FIFO. The
                    # descriptor check also rejects a replacement after lstat.
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                    with os.fdopen(os.open(part, flags), "rb") as source:
                        current = os.fstat(source.fileno())
                        if (not stat.S_ISREG(current.st_mode) or current.st_size != chunk_bytes or
                                (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)):
                            raise FirmwareInstallError("Backup fragment changed before reading")
                        remaining = chunk_bytes
                        while remaining:
                            block = source.read(min(64 * 1024, remaining))
                            if not block:
                                raise FirmwareInstallError("Backup fragment was truncated while reading")
                            output.write(block)
                            total += len(block)
                            remaining -= len(block)
                        if source.read(1):
                            raise FirmwareInstallError("Backup fragment grew while reading")
                except Exception as error:
                    raise FirmwareInstallError(f"Flash backup range {offset // chunk_bytes + 1}/8 failed: {error}") from error
            if total != FLASH_BYTES:
                raise FirmwareInstallError("The assembled full flash backup has an invalid size")
            command_timeout()
        # Retain our bounded fragments and any partial output for diagnosis.
        # This is still an UNVERIFIED backup. The installer fsyncs the assembled
        # file and independently verifies all 8 MiB before authorizing any write.

    @staticmethod
    def _image_timeout(image: Path) -> int:
        # Full recovery loads include erase/program plus load -v; leave enough
        # time for slow full-speed USB without shortening the rollback window.
        return 600 if image.stat().st_size >= FLASH_BYTES else 180

    def verify(self, target: BootDevice, image: Path, address: int | None = None) -> None:
        self._command(["verify", str(image), *(["-o", hex(address)] if address is not None else []),
                       *self._target_arguments(target)], timeout=self._image_timeout(image))

    def load(self, target: BootDevice, image: Path, address: int | None = None) -> None:
        self._command(["load", "-v", str(image), *(["-o", hex(address)] if address is not None else []),
                       *self._target_arguments(target)], timeout=self._image_timeout(image))

    def reboot(self, target: BootDevice) -> None:
        self._command(["reboot", *self._target_arguments(target)], timeout=20)
