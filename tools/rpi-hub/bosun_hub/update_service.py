"""Persistent, bounded update jobs owned by the Pi, independent of the editor.

The existing line transport carries an archive to the Pi. Only a fully received,
validated package can acquire the USB link. Disconnecting an editor never
cancels a flash operation or deletes its verified recovery image.

A ``kind: "pi"`` job carries a signed Pi package instead. The hub (user bosun)
only stages it and rewrites a request file; root's bosun-pi-update.path checks
the signature, installs it and reports in its own world-readable status file,
which STATUS follows across the hub restart the installation causes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time

from . import __version__

MAX_ARCHIVE = 4 * 1024 * 1024
MAX_CHUNK = 16 * 1024
TERMINAL = {"done", "error", "rolled-back", "recovery-required", "aborted"}
log = logging.getLogger("bosun_hub.updates")
PI_PACKAGE, PI_SIGNATURE = "package.tar.gz", "package.sig"
MAX_SIGNATURE = 4 * 1024
PI_PHASES = {"installing", "done", "rolled-back", "error"}
# The hub's StateDirectory holds the request; root writes <job>.json results.
PI_REQUEST_DIR = Path("/var/lib/bosun-hub/pi-update")
PI_STATUS_DIR = Path("/var/lib/bosun-pi-update")
# Self-update needs the root installer, its trigger and the trust anchor.
PI_INSTALLER = (Path("/etc/systemd/system/bosun-pi-update.path"),
                Path("/etc/systemd/system/bosun-pi-update.service"),
                Path("/usr/local/libexec/bosun-pi-update"), Path("/etc/bosun/allowed_signers"))
# Root records its first status within seconds of the request.
PI_START_TIMEOUT = 300


class UpdateService:
    def __init__(self, hub, *, root=None, installer=None, recovery_verifier=None,
                 pi_request_dir=PI_REQUEST_DIR, pi_status_dir=PI_STATUS_DIR, pi_installer=PI_INSTALLER):
        self.hub = hub
        self.root = Path(root or Path.home() / ".local/state/bosun/updates")
        self.installer = installer
        self.recovery_verifier = recovery_verifier
        self.pi_request_dir = Path(pi_request_dir)
        self.pi_status_dir = Path(pi_status_dir)
        self.pi_installer = tuple(Path(path) for path in pi_installer)
        self.active = None
        self.task = None
        self.maintenance = False
        self._stopping = False
        self._volatile_status = None
        self._recovery_jobs = set()
        # Unfinished Pi jobs (job -> created): Desktop follows them with PING.
        self._pi_jobs = {}
        self._recovery_scan_complete = False
        # {"phase", "detail"} of the running job for Stage's HUB frames.
        self.progress = None

    def _announce(self, state=None):
        """Report a running job's step to Stage, or its end without a state.

        An upload owns nothing yet and may be abandoned, so it never covers
        Stage; the screen appears once a committed job starts.
        """
        if state is not None and state["job"] != self.active:
            return  # a late callback from a job that has already ended
        progress = None
        if state is not None and state["phase"] not in TERMINAL | {"uploading"}:
            progress = {"phase": state["phase"], "detail": state.get("detail") or ""}
        if progress != self.progress:
            self.progress = progress
            self.hub._update_progress()

    def _remember_recovery(self, state):
        if state["phase"] == "recovery-required":
            self._recovery_jobs.add(state["job"])
        else:
            self._recovery_jobs.discard(state["job"])
        if state.get("kind") == "pi" and state["phase"] in {"uploading", "installing"}:
            self._pi_jobs[state["job"]] = state.get("created", 0)
        else:
            self._pi_jobs.pop(state["job"], None)

    def _pi_pending(self):
        """Whether a Pi job is still running; an abandoned upload expires like BEGIN's."""
        for job, created in list(self._pi_jobs.items()):
            try:
                state = self._load(job)  # follows root's status and updates the cache
            except (OSError, ValueError, KeyError, TypeError):
                self._pi_jobs.pop(job, None)
                continue
            if state["phase"] == "installing" or (
                    state["phase"] == "uploading" and time.time() - created < 900):
                return True
        return False

    def allows_local_ping(self):
        """Keep recovery reachable without disguising normal Captain outages.

        A restarted desktop needs its initial PING before it can ask for the
        saved job. BOOTSEL cannot answer that PING, so unresolved jobs permit
        a hub ACK; so does a Pi update, which restarts the hub and needs no
        Captain. Scan disk once, then maintain the cache with job transitions.
        """
        if self.maintenance:
            return True
        if not self._recovery_scan_complete:
            self._recovery_scan_complete = True
            try:
                for directory in self.root.iterdir():
                    if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                        continue
                    try:
                        self._load(directory.name)
                    except (OSError, ValueError, KeyError, TypeError):
                        # An unreadable existing job is not evidence that its
                        # flash transaction completed. Preserve access to it.
                        self._recovery_jobs.add(directory.name)
                        log.exception("Cannot inspect saved update job %s", directory.name)
            except FileNotFoundError:
                pass  # A hub that has never updated has no recovery state.
            except OSError:
                log.exception("Cannot inspect update directory")
        return bool(self._recovery_jobs) or self._pi_pending()

    def supported(self):
        return (sys.platform == "linux" and shutil.which("picotool") is not None
                and (self.hub.link.port_name or "").startswith("/dev/"))

    def pi_supported(self):
        """The root installer and its trust anchor exist, and the request is writable."""
        return (all(path.is_file() for path in self.pi_installer)
                and self.pi_request_dir.is_dir() and os.access(self.pi_request_dir, os.W_OK))

    def hub_info(self):
        return {"type": "HUB_INFO", "version": __version__, "pi_update": self.pi_supported()}

    def start(self):
        """Ask root again for a Pi job left installing by a reboot or a hub restart.

        Root rolls back an interrupted run; while a run is active the repeated
        request is a no-op, and a finished job is never installed twice.
        """
        pending = []
        try:
            for directory in self.root.iterdir():
                if directory.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                    continue
                try:
                    saved = json.loads((directory / "status.json").read_text(encoding="utf-8"))
                    if saved.get("kind") != "pi" or saved.get("phase") != "installing":
                        continue  # Captain jobs are inspected on demand
                    state = self._load(directory.name)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
                if state["phase"] == "installing":
                    pending.append(state)
        except OSError:
            return
        if pending and self.pi_supported():
            newest = max(pending, key=lambda state: state.get("committed", state["created"]))
            try:
                self._request_pi(newest["job"])
            except OSError:
                log.exception("Cannot ask the Pi installer about job %s", newest["job"])

    def _directory(self, job):
        if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9]{32}", job):
            raise ValueError("Invalid update job")
        directory = self.root / job
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unknown update job")
        return directory

    def _save(self, state):
        directory = self._directory(state["job"])
        temporary = directory / "status.tmp"
        with temporary.open("w", encoding="utf-8") as out:
            json.dump(state, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(directory / "status.json")
        # Native installation is Linux-only. Windows hosts can exercise the
        # upload protocol in offline tests but cannot open directories for fsync.
        if os.name != "nt":
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._remember_recovery(state)

    def _load(self, job):
        directory = self._directory(job)
        if self._volatile_status and self._volatile_status["job"] == job:
            state = dict(self._volatile_status)
            self._remember_recovery(state)
            return state
        try:
            state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            # BEGIN creates the directory/package before its first durable
            # status. A crash there never acquired USB and must not prevent
            # all future updates. Retain the orphan for inspection; unknown
            # files or any installation directory require recovery instead.
            pre_upload_only = all(
                entry.name in {"package.zip", PI_PACKAGE, PI_SIGNATURE, "status.tmp"}
                and entry.is_file() and not entry.is_symlink()
                for entry in directory.iterdir()
            )
            state = {"job": job, "phase": "aborted" if pre_upload_only else "recovery-required",
                     "error": ("The hub stopped before the upload job was saved. Start a new update."
                               if pre_upload_only else
                               "The update status is missing, but installation evidence remains. Check the saved recovery image before another update.")}
            self._remember_recovery(state)
            return state
        if state.get("job") != job:
            raise ValueError("Invalid update journal")
        if state.get("kind") == "pi":
            state = self._pi_state(state)
            self._remember_recovery(state)
            return state
        if state["phase"] not in TERMINAL | {"uploading"} and self.active != job:
            state.update(phase="recovery-required", error="The hub restarted during an update. The saved recovery image must be checked before another update.")
            self._remember_recovery(state)
            self._save(state)
        self._remember_recovery(state)
        return state

    def _pi_state(self, state):
        """A committed Pi job follows root's status; the hub keeps its final result."""
        if state["phase"] != "installing":
            return state
        try:
            result = json.loads((self.pi_status_dir / f"{state['job']}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            result = None
        except (OSError, ValueError):
            return state  # root replaces the file atomically; ask again later
        if (isinstance(result, dict) and result.get("job") == state["job"]
                and result.get("phase") in PI_PHASES):
            state.update(phase=result["phase"], version=result.get("version"),
                         error=str(result.get("error") or ""))
            if isinstance(result.get("detail"), str):
                state["detail"] = result["detail"]
        elif result is None and time.time() - state.get("committed", state["created"]) > PI_START_TIMEOUT:
            state.update(phase="error", error="The Pi installer did not start. "
                         "Check bosun-pi-update.path on the Pi, then start the update again.")
        if state["phase"] != "installing":
            self._save(state)
        return state

    def _request_pi(self, job):
        # Rewritten in place like the boot logo trigger: closing the written
        # file is the change bosun-pi-update.path reacts to.
        with (self.pi_request_dir / "request").open("w", encoding="ascii", newline="\n") as out:
            out.write(job + "\n")
            out.flush()
            os.fsync(out.fileno())

    @staticmethod
    def _payload(state):
        return PI_PACKAGE if state.get("kind") == "pi" else "package.zip"

    def _reply(self, sub, request, state):
        self.hub._offer_correlated(sub, "id" in request, request.get("id"),
                                   {"type": "HUB_UPDATE", **state})

    def _error(self, sub, request, error):
        self.hub._offer_correlated(sub, "id" in request, request.get("id"),
                                   {"type": "ERROR", "of": request.get("type"), "error": str(error)})

    def handle(self, sub, request):
        kind = request.get("type", "")
        if not isinstance(kind, str) or not kind.startswith("HUB_UPDATE_"):
            return False
        try:
            if self._stopping:
                raise ValueError("Hub is stopping")
            if kind == "HUB_UPDATE_INFO":
                self._reply(sub, request, {"phase": "ready", "supported": self.supported()})
                return True
            if kind == "HUB_UPDATE_BEGIN":
                self._begin(sub, request)
                return True
            state = self._load(request.get("job"))
            if kind == "HUB_UPDATE_STATUS":
                self._reply(sub, request, state)
            elif kind == "HUB_UPDATE_CHUNK":
                self._chunk(sub, request, state)
            elif kind == "HUB_UPDATE_ABORT":
                if state["phase"] != "uploading":
                    raise ValueError("An update that has started cannot be cancelled")
                state["phase"] = "aborted"
                self._save(state)
                if self.active == state["job"]:
                    self.active = None
                self._reply(sub, request, state)
            elif kind == "HUB_UPDATE_COMMIT":
                self._commit(sub, request, state)
            elif kind == "HUB_UPDATE_RECOVER":
                self._recover(sub, request, state)
            else:
                raise ValueError("Unknown hub update command")
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._error(sub, request, error)
        return True

    def _begin(self, sub, request):
        pi = request.get("kind") == "pi"
        if "kind" in request and not pi:
            raise ValueError("Unknown update kind")
        if pi:
            if not self.pi_supported():
                raise ValueError("This hub cannot update itself; update the Pi with the Setup guide command")
            signature = request.get("signature")
            signature = signature.strip() if isinstance(signature, str) else ""
            if (not signature.isascii() or len(signature) > MAX_SIGNATURE
                    or not signature.startswith("-----BEGIN SSH SIGNATURE-----")
                    or not signature.endswith("-----END SSH SIGNATURE-----")):
                raise ValueError("Invalid Pi package signature")
        elif not self.supported():
            raise ValueError("This hub is not ready to update the Captain")
        if self.active:
            state = self._load(self.active)
            if state["phase"] not in TERMINAL:
                raise ValueError("An update is already in progress")
        size, digest = request.get("size"), request.get("sha256")
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
            raise ValueError("Update archive exceeds the size limit")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid update SHA-256")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Inspect previous jobs before admission: a restart cannot turn a
        # partial flash into permission to overwrite its recovery evidence.
        for directory in self.root.iterdir():
            if directory.is_dir() and re.fullmatch(r"[a-f0-9]{32}", directory.name):
                previous = self._load(directory.name)
                # A Pi update keeps Captain recovery evidence and may bring its fix.
                if previous["phase"] == "recovery-required" and not pi:
                    raise ValueError("An interrupted update requires recovery first")
                if previous["phase"] == "uploading":
                    if time.time() - previous["created"] < 900:
                        raise ValueError("An upload is pending; resume or cancel that job")
                    previous["phase"] = "aborted"
                    self._save(previous)
                elif previous["phase"] not in TERMINAL | {"recovery-required"}:
                    raise ValueError("An update is already in progress")
        if pi:
            # Root's private copy, the extracted tree and the rollback snapshot.
            if shutil.disk_usage(self.root).free < 128 * 1024 * 1024 + size:
                raise ValueError("Insufficient disk space for the Pi update")
        elif shutil.disk_usage(self.root).free < 4 * 8 * 1024 * 1024 + size:
            raise ValueError("Insufficient disk space for a verified full recovery image")
        job = secrets.token_hex(16)
        (self.root / job).mkdir(mode=0o700)
        state = {"job": job, "phase": "uploading", "size": size, "sha256": digest,
                 "received": 0, "created": time.time()}
        if pi:
            state["kind"] = "pi"
        with (self.root / job / self._payload(state)).open("xb"):
            pass
        if pi:
            with (self.root / job / PI_SIGNATURE).open("xb") as out:
                out.write(signature.encode("ascii") + b"\n")
                out.flush()
                os.fsync(out.fileno())
        self._save(state)
        self.active = job
        self._reply(sub, request, state)

    def _chunk(self, sub, request, state):
        if state["phase"] != "uploading":
            raise ValueError("This job is no longer accepting data")
        if self.active not in (None, state["job"]):
            raise ValueError("Another update owns the device")
        offset, encoded = request.get("offset"), request.get("data")
        if type(offset) is not int or offset < 0 or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_CHUNK + 2) // 3):
            raise ValueError("Invalid update chunk")
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_CHUNK or offset + len(data) > state["size"]:
            raise ValueError("Update chunk exceeds the archive bounds")
        path = self._directory(state["job"]) / self._payload(state)
        with path.open("r+b") as out:
            if offset < state["received"]:
                # A lost reply can safely retry exactly the same bytes.
                out.seek(offset)
                if offset + len(data) > state["received"] or out.read(len(data)) != data:
                    raise ValueError("Conflicting update chunk retry")
            else:
                if offset != state["received"]:
                    raise ValueError("Update chunk is out of order")
                out.seek(offset)
                out.write(data)
                out.truncate()
                out.flush()
                os.fsync(out.fileno())
                state["received"] += len(data)
                self._save(state)
        self.active = state["job"]
        self._reply(sub, request, state)

    def _commit(self, sub, request, state):
        if state["phase"] != "uploading":
            # Commit ACK loss must not start a second flash transaction.
            self._reply(sub, request, state)
            return
        if self.active not in (None, state["job"]):
            raise ValueError("Another update owns the device")
        pi = state.get("kind") == "pi"
        for directory in self.root.iterdir():
            if directory.name != state["job"] and directory.is_dir() and re.fullmatch(r"[a-f0-9]{32}", directory.name):
                phase = self._load(directory.name)["phase"]
                if phase == "recovery-required" and not pi:
                    raise ValueError("An interrupted update requires recovery first")
                if phase == "installing":
                    raise ValueError("A Pi update is in progress")
        if state["received"] != state["size"]:
            raise ValueError("Update archive is incomplete")
        if pi:
            self._commit_pi(sub, request, state)
            return
        if not self.hub.link.connected or not self.hub._update_device_info:
            raise ValueError("Read the connected Captain firmware before updating")
        port = self.hub.link.port_name
        if not port or not port.startswith("/dev/"):
            raise ValueError("Update requires a Captain connected to this Pi by USB")
        identity = self.hub.link.usb_identity
        if not identity:
            raise ValueError("The hub cannot verify the connected Captain USB identity")
        state["phase"] = "validating"
        self._save(state)
        self.active = state["job"]
        self._announce(state)
        self.task = asyncio.create_task(self._run(state, dict(self.hub._update_device_info), port, identity))
        self._reply(sub, request, state)

    def _commit_pi(self, sub, request, state):
        """Hand a complete Pi package to root; the Captain is not involved."""
        if not self.pi_supported():
            raise ValueError("This hub cannot update itself; update the Pi with the Setup guide command")
        directory = self._directory(state["job"])
        data = (directory / PI_PACKAGE).read_bytes()
        if len(data) != state["size"] or hashlib.sha256(data).hexdigest() != state["sha256"]:
            state.update(phase="error", error="The Pi package failed its SHA-256 check")
        elif not (directory / PI_SIGNATURE).is_file():
            state.update(phase="error", error="The Pi package signature is missing")
        else:
            state.update(phase="installing", committed=time.time(), detail="Waiting for the Pi installer")
        self._save(state)
        self.active = None
        if state["phase"] == "installing":
            try:
                self._request_pi(state["job"])
            except OSError as error:
                state.update(phase="error", error=f"Cannot start the Pi installer: {error}")
                self._save(state)
        self._reply(sub, request, state)

    def _recover(self, sub, request, state):
        if ((self.active == state["job"] and state.get("operation") == "recovery")
                or state.get("recovery_verified") is True):
            self._reply(sub, request, state)  # Lost ACK must not start a second check.
            return
        if state["phase"] != "recovery-required":
            raise ValueError("This job does not require recovery verification")
        if self.active is not None or (self.task is not None and not self.task.done()):
            raise ValueError("Another update owns the device")
        if not self.hub.link.connected:
            raise ValueError("Reconnect the original Captain before checking recovery")
        port, identity = self.hub.link.port_name, self.hub.link.usb_identity
        if not port or not port.startswith("/dev/") or not identity:
            raise ValueError("The hub cannot verify the connected Captain USB identity")
        from .firmware_install import FirmwareInstallError, PinnedDevice, read_prewrite_recovery
        try:
            journal, _ = read_prewrite_recovery(self._directory(state["job"]) / "installation")
            if PinnedDevice(**journal["device"]) != PinnedDevice(*identity):
                raise ValueError("Reconnect the original Captain to its original USB port before checking recovery")
        except FirmwareInstallError as error:
            raise ValueError(str(error)) from error
        state.update(phase="verifying", operation="recovery", recovery_verified=False,
                     recovery_error="", detail="Checking the original firmware and saved configuration")
        self._save(state)
        if self._volatile_status and self._volatile_status["job"] == state["job"]:
            self._volatile_status = None
        self.active = state["job"]
        self.maintenance = True
        self._announce(state)
        self.task = asyncio.create_task(self._run_recovery(state, port, identity))
        self._reply(sub, request, state)

    async def _run_recovery(self, state, port, identity):
        stopped = False
        loop = asyncio.get_running_loop()
        try:
            from .firmware_install import PinnedDevice, verify_prewrite_recovery
            deadline = loop.time() + 12
            while self.hub._request_pending or self.hub._upstream_reads:
                if loop.time() >= deadline:
                    raise ValueError("Captain is busy; retry after current operations finish")
                await asyncio.sleep(0.05)
            await asyncio.to_thread(self.hub.link.stop)
            stopped = True
            if self.hub.link._thread is not None:
                raise ValueError("The hub could not release the Captain serial port")
            result = await asyncio.to_thread(self.recovery_verifier or verify_prewrite_recovery,
                                            self._directory(state["job"]) / "installation", port,
                                            expected_device=PinnedDevice(*identity))
            if result.get("recovery_verified") is not True:
                raise ValueError("Recovery did not verify the original firmware and configuration")
            state.update(phase="error", **result, recovery_error="",
                         detail="Original firmware and all saved configuration verified unchanged. You can retry the update.")
        except Exception as error:
            state.update(phase="recovery-required", recovery_verified=False,
                         recovery_error=str(error), detail="Recovery check failed: " + str(error))
        finally:
            try:
                if stopped and self.hub.link._thread is None and not self._stopping:
                    self.hub._restart_update_link()
                    deadline = loop.time() + 20
                    while not self.hub.link.connected and loop.time() < deadline:
                        await asyncio.sleep(0.1)
                    if not self.hub.link.connected:
                        raise ValueError("The hub has not reconnected to the Captain. Reconnect it and check recovery again.")
                    if self.hub.link.usb_identity != identity:
                        raise ValueError("The Captain USB identity changed while the hub reconnected")
            except Exception as error:
                state.update(phase="recovery-required", recovery_verified=False,
                             recovery_error=str(error), detail="Recovery check failed: " + str(error))
            try:
                self._save(state)
                self._volatile_status = None
            except OSError as error:
                state.update(phase="recovery-required", recovery_verified=False,
                             recovery_error=str(error), detail="Cannot persist the recovery check. Its original evidence has been retained.")
                self._volatile_status = dict(state)
                log.exception("Cannot persist recovery verification result")
            finally:
                self._remember_recovery(state)
                self.maintenance = False
                self.active = None
                self._announce()

    async def _run(self, state, info, port, identity):
        stopped = False
        loop = asyncio.get_running_loop()
        def progress(snapshot):
            def apply():
                raw_phase = snapshot.get("phase", "verifying")
                phase = {"preflight": "validating", "reading_configuration": "backing-up",
                         "entering_bootloader": "backing-up", "backing_up_flash": "backing-up",
                         "backup_verified": "backing-up", "flashing_firmware": "flashing",
                         "verifying_configuration": "verifying", "rolling_back": "verifying",
                         "complete": "verifying", "failed": "verifying", "rolled_back": "verifying",
                         "manual_recovery": "verifying"}.get(raw_phase, raw_phase)
                state.update(phase=phase, detail=snapshot.get("detail", raw_phase.replace("_", " ")))
                if snapshot.get("backup_path"):
                    state["backup"] = snapshot["backup_path"]
                try:
                    self._save(state)
                except OSError:
                    # The installer's own fsynced journal gates each physical
                    # write. A UI-status failure cannot cancel its rollback.
                    self._volatile_status = dict(state)
                    log.exception("Cannot persist update progress")
                self._announce(state)
            loop.call_soon_threadsafe(apply)
        try:
            from .update_package import validate_update_package
            from .firmware_install import FirmwareInstaller, PinnedDevice
            package_path = self._directory(state["job"]) / "package.zip"
            data = await asyncio.to_thread(package_path.read_bytes)
            if len(data) != state["size"] or hashlib.sha256(data).hexdigest() != state["sha256"]:
                raise ValueError("Update archive failed its SHA-256 check")
            package = await asyncio.to_thread(validate_update_package, package_path)
            state.update(release=package.manifest["release"], firmware_version=package.manifest["firmware_version"])
            self._save(state)
            self.maintenance = True
            # Admit no new device commands, then let previously accepted
            # requests drain before closing the single owned serial port.
            deadline = loop.time() + 12
            while self.hub._request_pending or self.hub._upstream_reads:
                if loop.time() >= deadline:
                    raise ValueError("Captain is busy; retry after current operations finish")
                await asyncio.sleep(0.05)
            await asyncio.to_thread(self.hub.link.stop)
            stopped = True
            if self.hub.link._thread is not None:
                raise ValueError("The hub could not release the Captain serial port")
            installer = self.installer or FirmwareInstaller()
            result = await asyncio.to_thread(installer.run, package_path, info, port,
                                            self._directory(state["job"]) / "installation", progress,
                                            expected_device=PinnedDevice(*identity))
            # Flush callbacks posted by the worker before writing terminal state.
            await asyncio.sleep(0)
            phase = {"complete": "done", "failed": "error", "rolled_back": "rolled-back",
                     "manual_recovery": "recovery-required"}.get(result.get("status"), "error")
            state.update(phase=phase, error=result.get("error", ""))
            if result.get("backup_path"):
                state["backup"] = str(result["backup_path"])
        except Exception as error:
            phase = "error"
            journal = self._directory(state["job"]) / "installation/journal.json"
            if journal.exists():
                try:
                    if json.loads(journal.read_text(encoding="utf-8")).get("flash_may_be_modified"):
                        phase = "recovery-required"
                except (OSError, ValueError):
                    phase = "recovery-required"
            state.update(phase=phase, error=str(error))
        finally:
            try:
                if stopped and self.hub.link._thread is None and not self._stopping:
                    self.hub._restart_update_link()
                    deadline = loop.time() + 20
                    while not self.hub.link.connected and loop.time() < deadline:
                        await asyncio.sleep(0.1)
                    if not self.hub.link.connected:
                        state["detail"] = "The firmware transaction finished, but the hub has not reconnected. Reconnect the Captain."
                self._save(state)
                self._volatile_status = None
            except OSError as error:
                state.update(phase="recovery-required", error=f"Cannot persist the update result: {error}")
                self._volatile_status = dict(state)
                log.exception("Cannot persist update result; recovery evidence remains in installation journal")
            finally:
                self._remember_recovery(state)
                self.maintenance = False
                self.active = None
                self._announce()

    def stop(self):
        self._stopping = True
        # A worker may be inside a verified flash/restore. Never cancel it.
        # The service manager must allow sufficient graceful shutdown time.

    async def wait(self):
        if self.task is not None:
            await asyncio.shield(self.task)
