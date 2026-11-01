#!/usr/bin/python3 -I
"""Install a signed Bosun Pi package staged by the hub; --root supports fixture tests.

Runs as root from bosun-pi-update.service when the hub rewrites
/var/lib/bosun-hub/pi-update/request with a job id. Hub files belong to the
network-facing bosun account and are untrusted input: the package is copied
into a root-only directory, its OpenSSH signature is checked against
/etc/bosun/allowed_signers before anything is extracted, and everything
install.sh replaces is saved first, so a failed installation or health check
restores the previous release. Each job's result is in the world-readable
/var/lib/bosun-pi-update/<job>.json.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import fnmatch
import gzip
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import threading
import time
from types import SimpleNamespace

HUB_USER = "bosun"
JOBS = ".local/state/bosun/updates"  # the hub's update jobs, below its home
REQUEST_DIR, REQUEST = "var/lib/bosun-hub/pi-update", "request"
STATE = "var/lib/bosun-pi-update"
ALLOWED_SIGNERS = "etc/bosun/allowed_signers"
IDENTITY, NAMESPACE = "bosun-release", "bosun-pi-package"
PACKAGE, SIGNATURE = "package.tar.gz", "package.sig"
MAX_PACKAGE, MAX_SIGNATURE, MAX_STATUS = 4 * 1024 * 1024, 4 * 1024, 64 * 1024
MAX_MEMBERS, MAX_EXTRACTED = 4096, 64 * 1024 * 1024
# What install.sh (and install-boot-splash.sh, which it reruns) replaces.
SNAPSHOT = ("opt/bosun-hub", "etc/systemd/system/bosun-*", "etc/systemd/system/*.d/*bosun*",
            "etc/systemd/system/*.wants/bosun-*", "etc/udev/rules.d/*bosun*",
            "usr/local/libexec/bosun-*", ALLOWED_SIGNERS)
SERVICES = ("bosun-hub.service", "bosun-kiosk.service", "bosun-midi.timer")
CAPTAIN_ACTIVE = {"validating", "backing-up", "flashing", "rebooting", "verifying"}
TERMINAL = {"done", "rolled-back", "error"}
STAGING = ".bosun-"  # restore's temporary names, never part of a snapshot
JOB = re.compile(r"[a-f0-9]{32}")
VERSION = re.compile(r'^__version__ = "([0-9A-Za-z.+-]{1,64})"\s*$', re.MULTILINE)
ATTEMPTS = 3
# Shorter than the hub's PI_START_TIMEOUT, so root decides before the hub
# reports a job that never started.
REQUEST_TIMEOUT = 240


# -- untrusted hub files ---------------------------------------------------

def open_directory(path, directory=None):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=directory)


def read_file(directory, name, limit):
    """A regular file below a directory fd, or None when it is absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"Refusing unexpected file {name}")
        chunks, total = [], 0
        while chunk := os.read(fd, min(1 << 16, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"Refusing unexpected file {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def open_jobs(ctx, *names):
    """The hub's job directory (or one job), walked without following links."""
    fd = os.open(ctx.home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in (*JOBS.split("/"), *names):
            child = open_directory(part, fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_request(ctx):
    """The requested job and the request's age in seconds, or None."""
    try:
        directory = open_directory(ctx.root / REQUEST_DIR)
    except FileNotFoundError:
        return None
    try:
        data = read_file(directory, REQUEST, 256) or b""
        written = os.stat(REQUEST, dir_fd=directory, follow_symlinks=False).st_mtime if data else 0
    finally:
        os.close(directory)
    job = data.decode("ascii", "replace").strip()
    if not JOB.fullmatch(job):
        if data:
            print("Ignoring an invalid update request", flush=True)
        return None
    return job, time.time() - written


def captain_busy(ctx):
    """Whether a Captain firmware job is running: a hub restart would cut it off."""
    try:
        jobs = open_jobs(ctx)
    except FileNotFoundError:
        return False
    try:
        for name in os.listdir(jobs):
            if not JOB.fullmatch(name):
                continue
            try:
                job = open_directory(name, jobs)
            except OSError:
                continue  # a link or a vanished job holds no running update
            try:
                state = json.loads(read_file(job, "status.json", MAX_STATUS) or b"null")
            except (OSError, ValueError):
                continue  # the hub reports such a job as needing recovery, not running
            finally:
                os.close(job)
            if (isinstance(state, dict) and state.get("kind") != "pi"
                    and state.get("phase") in CAPTAIN_ACTIVE):
                return True
    finally:
        os.close(jobs)
    return False


def stage(ctx, job, work):
    """Root-only copies: the hub's files are never read again after this."""
    try:
        source = open_jobs(ctx, job)
    except FileNotFoundError:
        raise ValueError("The hub has not staged this Pi package") from None
    try:
        package = read_file(source, PACKAGE, MAX_PACKAGE)
        signature = read_file(source, SIGNATURE, MAX_SIGNATURE)
    finally:
        os.close(source)
    if not package or not signature:
        raise ValueError("The hub has not staged a complete Pi package")
    for name, data in ((PACKAGE, package), (SIGNATURE, signature)):
        with open(work / name, "xb") as out:
            out.write(data)


# -- signature and extraction ----------------------------------------------

def verify(ctx, work):
    allowed = ctx.root / ALLOWED_SIGNERS
    try:
        info = os.stat(allowed, follow_symlinks=False)
    except FileNotFoundError:
        raise ValueError(f"{allowed} is missing; update the Pi with the Setup guide command") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(f"Refusing {allowed}: it must be a root-owned file "
                         "not writable by group or others")
    try:
        with open(work / PACKAGE, "rb") as message:
            result = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", IDENTITY,
                 "-n", NAMESPACE, "-s", str(work / SIGNATURE)],
                stdin=message, capture_output=True, text=True, errors="replace", timeout=120)
    except FileNotFoundError:
        raise ValueError("ssh-keygen is missing; install openssh-client") from None
    lines = (result.stderr + result.stdout).strip().splitlines()
    if result.returncode != 0:
        raise ValueError("The package is not signed with the Bosun release key"
                         + (f" ({lines[-1]})" if lines else ""))
    print(*lines, sep="\n", flush=True)


class Capped:
    """A decompressed stream that refuses to grow past its limit."""

    def __init__(self, stream, limit):
        self.stream, self.left = stream, limit

    def read(self, size=-1):
        data = self.stream.read(self.left + 1 if size < 0 or size > self.left + 1 else size)
        self.left -= len(data)
        if self.left < 0:
            raise ValueError("The package expands beyond its size limit")
        return data


def extract(archive, tree):
    """Regular files and directories only, below the tree, within the caps."""
    os.mkdir(tree, 0o700)
    seen, total = set(), 0
    with gzip.open(archive, "rb") as compressed, tarfile.open(
            fileobj=Capped(compressed, MAX_EXTRACTED + MAX_MEMBERS * 4096), mode="r|") as tar:
        for count, member in enumerate(tar, 1):
            if count > MAX_MEMBERS:
                raise ValueError("The package holds too many files")
            name = member.name.rstrip("/") if member.isdir() else member.name
            parts = name.split("/")
            if (not name or name.startswith("/") or "\\" in name
                    or any(part in ("", ".", "..") for part in parts)):
                raise ValueError(f"Refusing package member {member.name!r}: unsafe path")
            if not (member.isreg() or member.isdir()):
                raise ValueError(f"Refusing package member {name}: not a regular file or directory")
            if name in seen:
                raise ValueError(f"Refusing package member {name}: duplicate")
            seen.add(name)
            total += member.size
            if total > MAX_EXTRACTED:
                raise ValueError("The package expands beyond its size limit")
            # Only this run creates entries in the root-only tree, all of them
            # directories or new files, so no component can be a link.
            target = tree.joinpath(*parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o755, exist_ok=True)
                continue
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o755 if member.mode & 0o100 else 0o644)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(tar.extractfile(member), out)


def package_version(tree):
    try:
        match = VERSION.search((tree / "tools/rpi-hub/bosun_hub/__init__.py").read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError):
        match = None
    if match is None or not (tree / "tools/rpi-hub/quick-install.sh").is_file():
        raise ValueError("This is not a Bosun Pi package")
    return match.group(1)


# -- snapshot and restore --------------------------------------------------

def expand(root, parts, prefix=""):
    head, rest = parts[0], parts[1:]
    if any(char in head for char in "*?["):
        try:
            names = sorted(name for name in os.listdir(root / prefix if prefix else root)
                           if fnmatch.fnmatchcase(name, head) and not name.startswith(STAGING))
        except (FileNotFoundError, NotADirectoryError):
            return []
    else:
        names = [head]
    found = []
    for name in names:
        path = f"{prefix}/{name}" if prefix else name
        if rest:
            if (root / path).is_dir() and not (root / path).is_symlink():
                found += expand(root, rest, path)
        elif os.path.lexists(root / path):
            found.append(path)
    return found


def installed(root):
    """Existing paths install.sh may replace, relative to root, none inside another."""
    found = {path for pattern in SNAPSHOT for path in expand(root, pattern.split("/"))}
    return sorted(path for path in found
                  if not any(path.startswith(other + "/") for other in found))


def keep(fd, info):
    # Fixture tests run unprivileged and cannot give files away.
    if os.geteuid() == 0:
        os.fchown(fd, info.st_uid, info.st_gid)
    os.fchmod(fd, stat.S_IMODE(info.st_mode))
    os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))


def copy_entry(source_dir, name, target_dir, target_name):
    """Copy one entry without following links, keeping its mode, owner and times.

    A directory gets its owner only after its contents: Stage belongs to the
    hub account, which must not be able to plant links while root writes.
    """
    info = os.stat(name, dir_fd=source_dir, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(name, dir_fd=source_dir), target_name, dir_fd=target_dir)
        if os.geteuid() == 0:
            os.chown(target_name, info.st_uid, info.st_gid, dir_fd=target_dir, follow_symlinks=False)
    elif stat.S_ISREG(info.st_mode):
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=source_dir)
        try:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise ValueError(f"{name} changed while it was being saved")
            target = os.open(target_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600, dir_fd=target_dir)
            try:
                while chunk := os.read(source, 1 << 20):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(target, view):]
                keep(target, info)
            finally:
                os.close(target)
        finally:
            os.close(source)
    elif stat.S_ISDIR(info.st_mode):
        source = open_directory(name, source_dir)
        try:
            os.mkdir(target_name, 0o700, dir_fd=target_dir)
            target = open_directory(target_name, target_dir)
            try:
                for child in sorted(os.listdir(source)):
                    copy_entry(source, child, target, child)
                keep(target, info)
            finally:
                os.close(target)
        finally:
            os.close(source)
    else:
        print(f"Skipping {name}: not a file, directory or link", flush=True)


def remove_at(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(name, dir_fd=directory)
    else:
        os.unlink(name, dir_fd=directory)


def remove(path):
    if os.path.lexists(path):
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            remove_at(parent, path.name)
        finally:
            os.close(parent)


def snapshot(root, work):
    files = work / "snapshot/files"
    files.mkdir(parents=True, mode=0o700)
    entries = installed(root)
    for entry in entries:
        relative = Path(entry)
        (files / relative.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
        source = os.open(root / relative.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        target = open_directory(files / relative.parent)
        try:
            copy_entry(source, relative.name, target, relative.name)
        finally:
            os.close(source)
            os.close(target)
    os.sync()
    # Written last: only a complete snapshot can be restored.
    with open(work / "snapshot/manifest.json", "x", encoding="utf-8") as out:
        json.dump({"patterns": SNAPSHOT, "entries": entries}, out)
        out.flush()
        os.fsync(out.fileno())
    return entries


def restore(root, work):
    entries = json.loads((work / "snapshot/manifest.json").read_text(encoding="utf-8"))["entries"]
    for path in installed(root):
        if path not in entries and not any(path.startswith(entry + "/") for entry in entries):
            remove(root / path)  # added by the failed release
    files = work / "snapshot/files"
    for entry in entries:
        relative = Path(entry)
        (root / relative.parent).mkdir(parents=True, exist_ok=True)
        parent = os.open(root / relative.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        source = open_directory(files / relative.parent)
        staged, replaced = STAGING + "restore-" + relative.name, STAGING + "replaced-" + relative.name
        try:
            # Build the copy beside its target, then swap it in by renaming.
            remove_at(parent, staged)
            copy_entry(source, relative.name, parent, staged)
            remove_at(parent, replaced)
            try:
                os.rename(relative.name, replaced, src_dir_fd=parent, dst_dir_fd=parent)
            except FileNotFoundError:
                pass
            os.rename(staged, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
            remove_at(parent, replaced)
        finally:
            os.close(parent)
            os.close(source)
    os.sync()


def tree_size(root, entries):
    total = 0
    for entry in entries:
        path = root / entry
        if path.is_dir() and not path.is_symlink():
            for directory, _, names in os.walk(path):
                total += sum(os.lstat(os.path.join(directory, name)).st_size for name in names)
        else:
            total += os.lstat(path).st_size
    return total


# -- installation and health ----------------------------------------------

def run_install(ctx, tree):
    """quick-install.sh from the verified tree; its output goes to the journal."""
    process = subprocess.Popen(["bash", str(tree / "tools/rpi-hub/quick-install.sh")], cwd=tree,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    tail = collections.deque(maxlen=1)

    def pump():
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            print(line, flush=True)
            if line.strip():
                tail.append(line.strip())

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=ctx.install_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise ValueError(f"quick-install.sh did not finish within {ctx.install_timeout:g} seconds") from None
    finally:
        reader.join(10)
    if code:
        raise ValueError(f"quick-install.sh failed with exit status {code}"
                         + (f": {tail[-1]}" if tail else ""))


def hub_info(address, timeout):
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as connection:
        connection.sendall(b'{"type":"HUB_INFO","id":"bosun-pi-update"}\n')
        buffer = b""
        while len(buffer) < 1 << 20:
            data = connection.recv(65536)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if isinstance(reply, dict) and reply.get("id") == "bosun-pi-update":
                    if reply.get("type") != "HUB_INFO":
                        raise ValueError(f"The hub does not answer HUB_INFO: {reply.get('error')}")
                    return reply
    raise ValueError("The hub closed the connection without answering HUB_INFO")


def healthy(ctx, version):
    """Services active and, after an install, the new hub answering for itself."""
    deadline = time.monotonic() + ctx.health_timeout
    while True:
        try:
            for service in SERVICES:
                if subprocess.run(["systemctl", "is-active", "--quiet", service],
                                  stdin=subprocess.DEVNULL, timeout=60).returncode:
                    raise ValueError(f"{service} is not active")
            if version is None:
                return
            info = hub_info(ctx.hub, min(10.0, ctx.health_timeout))
            if info.get("version") != version:
                raise ValueError(f"the hub reports version {info.get('version')}, not {version}")
            if info.get("pi_update") is not True:
                raise ValueError("the new hub reports that it cannot update itself")
            return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if time.monotonic() >= deadline:
                raise ValueError(f"Health check failed: {error}") from None
            time.sleep(1)


def restart(ctx):
    for command in (["systemctl", "daemon-reload"], ["udevadm", "control", "--reload"],
                    ["systemctl", "restart", *SERVICES]):
        subprocess.run(command, stdin=subprocess.DEVNULL, check=True, timeout=600)


# -- jobs ------------------------------------------------------------------

def publish(ctx, status):
    temporary = ctx.state / f".{status['job']}.json.tmp"
    with open(temporary, "w", encoding="utf-8") as out:
        json.dump(status, out)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, ctx.state / f"{status['job']}.json")
    print(f"Bosun Pi update {status['job']}: {status['phase']}, {status['detail']}"
          + (f" ({status['error']})" if status["error"] else ""), flush=True)


def step(ctx, status, detail):
    status["detail"] = detail
    publish(ctx, status)


def discard(work):
    try:
        remove(work)
    except OSError as error:
        print(f"Cannot remove {work}: {error}", flush=True)


def roll_back(ctx, status, work, failure):
    step(ctx, status, "Restoring the previous installation")
    try:
        restore(ctx.root, work)
        restart(ctx)
        healthy(ctx, None)
    except Exception as error:
        status.update(phase="error", detail="Manual recovery required",
                      error=f"{failure}. Restoring the previous installation failed: {error}. "
                      f"Its saved copy is in {work / 'snapshot'}")
        publish(ctx, status)
        return status
    status.update(phase="rolled-back", error=failure, detail="The previous installation was restored")
    publish(ctx, status)
    discard(work)
    return status


def install(ctx, status):
    work = ctx.state / status["job"]
    try:
        if captain_busy(ctx):
            raise ValueError("A Captain firmware update is running; update the Pi after it finishes")
        remove(work)
        os.mkdir(work, 0o700)
        stage(ctx, status["job"], work)
        verify(ctx, work)  # before a single byte of the archive is parsed
        step(ctx, status, "Unpacking the package")
        extract(work / PACKAGE, work / "tree")
        status["version"] = package_version(work / "tree")
        entries = installed(ctx.root)
        needed = 2 * tree_size(ctx.root, entries) + 32 * 1024 * 1024
        if shutil.disk_usage(ctx.state).free < needed:
            raise ValueError("Not enough free space to save the current installation")
        step(ctx, status, "Saving the current installation")
        snapshot(ctx.root, work)
    except Exception as error:
        status.update(phase="error", error=str(error) or type(error).__name__, detail="Nothing was changed")
        publish(ctx, status)
        discard(work)
        return status
    try:
        step(ctx, status, "Installing")
        run_install(ctx, work / "tree")
        step(ctx, status, "Checking the services")
        healthy(ctx, status["version"])
    except Exception as error:
        return roll_back(ctx, status, work, str(error) or type(error).__name__)
    status.update(phase="done", error=None, detail="Installed")
    publish(ctx, status)
    discard(work)
    return status


def handle(ctx, job, age):
    try:
        previous = json.loads((ctx.state / f"{job}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        previous = None
    except (OSError, ValueError):
        previous = {}  # unreadable: treat as interrupted, never as finished
    if isinstance(previous, dict) and previous.get("phase") in TERMINAL:
        return None  # a repeated request never installs a job twice
    status = {"job": job, "phase": "installing", "version": None, "error": None,
              "detail": "Checking the package"}
    if previous is None and age > REQUEST_TIMEOUT:
        # The hub has reported this job as never started; the startup run
        # must not install it behind Desktop's back.
        status.update(phase="error", error="The update request expired; start the update again",
                      detail="Nothing was changed")
        publish(ctx, status)
        return status
    if previous is None:
        publish(ctx, status)
        return install(ctx, status)
    # Left installing by a power loss or a stopped run: only this run holds the lock.
    work = ctx.state / job
    status["version"] = previous.get("version") if isinstance(previous, dict) else None
    if (work / "snapshot/manifest.json").is_file():
        return roll_back(ctx, status, work, "The update was interrupted before it finished")
    status.update(phase="error", error="The update was interrupted before anything was changed",
                  detail="Nothing was changed")
    publish(ctx, status)
    discard(work)
    return status


def check_owned(path):
    info = os.lstat(path)
    # geteuid() is root in the service; fixture tests run as their own user.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(f"Refusing to use {path}: it must be a root-owned directory "
                         "not writable by group or others")


def run(ctx):
    """Every job requested before this run ends; None while another run is active."""
    ctx.state.mkdir(parents=True, exist_ok=True, mode=0o755)
    check_owned(ctx.state)
    lock = os.open(ctx.state / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Bosun Pi update run is active", flush=True)
            return None
        results, seen = [], set()
        # Path events that arrive during a run are lost: catch up here.
        for _ in range(ATTEMPTS):
            request = read_request(ctx)
            if request is None or request[0] in seen:
                break
            seen.add(request[0])
            result = handle(ctx, *request)
            if result is not None:
                results.append(result)
        return results
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument("--home", type=Path, help="the hub account's home (default: from passwd)")
    parser.add_argument("--hub", default="127.0.0.1:9876", help="the hub's TCP protocol address")
    parser.add_argument("--health-timeout", type=float, default=120)
    parser.add_argument("--install-timeout", type=float, default=30 * 60)
    args = parser.parse_args(argv)
    try:
        home = args.home or args.root / pwd.getpwnam(HUB_USER).pw_dir.lstrip("/")
    except KeyError:
        parser.exit(1, f"Bosun Pi update: the {HUB_USER} account does not exist\n")
    ctx = SimpleNamespace(root=args.root, home=home, state=args.root / STATE, hub=args.hub,
                          health_timeout=args.health_timeout, install_timeout=args.install_timeout)
    try:
        results = run(ctx)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Bosun Pi update: {error}\n")
    failed = [result for result in results or () if result["phase"] != "done"]
    if failed:
        parser.exit(1, "".join(f"Bosun Pi update {result['job']}: {result['phase']}: {result['error']}\n"
                               for result in failed))


if __name__ == "__main__":
    main()
