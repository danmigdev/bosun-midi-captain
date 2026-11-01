"""The root startup-logo applier: exact pasting, pristine defaults and refusals."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "boot-splash/apply_logo.py"
spec = importlib.util.spec_from_file_location("boot_logo_applier", SOURCE)
applier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(applier)

posix = pytest.mark.skipif(
    os.name == "nt", reason="the root applier needs POSIX directory descriptors "
    "(dir_fd, O_NOFOLLOW, fchmod, ownership); run these tests on Linux or WSL")


def bsplash(width, height, seed):
    return (b"BSPLASH1" + struct.pack("<II", width, height)
            + random.Random(seed).randbytes(width * height * 3))


def pixel(data, width, x, y):
    start = 16 + (y * width + x) * 3
    return data[start:start + 3]


def composed(picture, logo):
    """Independent reference: logo rows over 120..519 x 28..249, the rest kept."""
    rows = []
    for y in range(360):
        row = picture[16 + y * 1920:16 + (y + 1) * 1920]
        if 28 <= y < 250:
            row = row[:360] + logo[16 + (y - 28) * 1200:16 + (y - 27) * 1200] + row[1560:]
        rows.append(row)
    return picture[:16] + b"".join(rows)


def test_paste_replaces_exactly_the_logo_box():
    picture, logo = bsplash(640, 360, 1), bsplash(400, 222, 2)
    out = applier.paste(picture, logo)
    assert out == composed(picture, logo) and len(out) == len(picture)
    assert pixel(out, 640, 119, 28) == pixel(picture, 640, 119, 28)
    assert pixel(out, 640, 120, 27) == pixel(picture, 640, 120, 27)
    assert pixel(out, 640, 120, 28) == pixel(logo, 400, 0, 0)
    assert pixel(out, 640, 519, 249) == pixel(logo, 400, 399, 221)
    assert pixel(out, 640, 520, 249) == pixel(picture, 640, 520, 249)
    assert pixel(out, 640, 519, 250) == pixel(picture, 640, 519, 250)
    # The version line (y=258) and the progress bar (y=294) stay.
    assert out[16 + 250 * 1920:] == picture[16 + 250 * 1920:]


@pytest.mark.parametrize("data", [
    bsplash(400, 222, 1)[:-1], b"BSPLASH0" + bsplash(400, 222, 1)[8:],
    b"BSPLASH1" + struct.pack("<II", 222, 400) + bsplash(400, 222, 1)[16:],
], ids=["size", "magic", "dimensions"])
def test_check_rejects_anything_but_the_exact_format(data):
    applier.check(bsplash(400, 222, 1), applier.LOGO, "logo.bin")
    with pytest.raises(ValueError, match="BSPLASH1"):
        applier.check(data, applier.LOGO, "logo.bin")


@pytest.fixture
def pi(tmp_path):
    root = tmp_path / "pi"
    assets = root / "opt/bosun-hub/boot-splash"
    hub = root / "var/lib/bosun-hub/boot-logo"
    for directory in (assets, hub):
        directory.mkdir(parents=True)
        directory.chmod(0o755)
    state = SimpleNamespace(root=root, assets=assets, hub=hub, pristine={})
    state.install = lambda seed: install(state, seed)
    state.install(10)
    return state


def install(pi, seed):
    """Fresh Bosun pictures, as install-boot-splash.sh or a trial commit writes them."""
    for index, name in enumerate(applier.PICTURES):
        pi.pristine[name] = bsplash(640, 360, seed + index)
        (pi.assets / name).write_bytes(pi.pristine[name])


def set_logo(pi, seed):
    logo = bsplash(400, 222, seed)
    (pi.hub / "logo.png").write_bytes(b"\x89PNG stored by the hub")
    (pi.hub / "logo.bin").write_bytes(logo)
    return logo


def clear_logo(pi):
    for name in ("logo.png", "logo.bin"):
        (pi.hub / name).unlink()


def run(pi):
    status = applier.run(pi.root)
    path = pi.root / "run/bosun-boot-logo/status.json"
    assert json.loads(path.read_text()) == status
    assert sorted(status) == ["error", "logo", "state"]
    return status


def pictures(pi):
    return {name: (pi.assets / name).read_bytes() for name in applier.PICTURES}


def defaults(pi):
    return {name: (pi.assets / "default" / name).read_bytes() for name in applier.PICTURES}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@posix
def test_custom_logo_is_pasted_into_every_picture(pi):
    logo = set_logo(pi, 1)
    assert run(pi) == {"state": "applied", "logo": sha(logo), "error": None}
    assert pictures(pi) == {name: composed(pi.pristine[name], logo) for name in applier.PICTURES}
    assert defaults(pi) == pi.pristine
    assert (pi.root / "run/bosun-boot-logo/status.json").stat().st_mode & 0o777 == 0o644
    assert all((pi.assets / name).stat().st_mode & 0o777 == 0o644 for name in applier.PICTURES)
    assert not [p for p in pi.assets.rglob(".bosun-logo-*")]


@posix
def test_use_bosun_logo_restores_the_defaults(pi):
    set_logo(pi, 1)
    run(pi)
    clear_logo(pi)
    assert run(pi) == {"state": "default", "logo": None, "error": None}
    assert pictures(pi) == pi.pristine


@posix
def test_only_a_complete_hub_pair_is_a_custom_logo(pi):
    set_logo(pi, 1)
    (pi.hub / "logo.png").unlink()
    assert run(pi)["state"] == "default"
    assert pictures(pi) == pi.pristine


@posix
def test_installer_refresh_becomes_the_new_default(pi):
    logo = set_logo(pi, 1)
    run(pi)
    pi.install(50)  # a new release refreshed the pictures, logo pixels gone
    assert run(pi)["state"] == "applied"
    assert defaults(pi) == pi.pristine
    assert pictures(pi) == {name: composed(pi.pristine[name], logo) for name in applier.PICTURES}
    clear_logo(pi)
    run(pi)
    assert pictures(pi) == pi.pristine


@posix
@pytest.mark.parametrize("crash", ["record", "first-picture", "second-picture"])
def test_interrupted_run_never_stores_a_custom_picture_as_default(pi, monkeypatch, crash):
    set_logo(pi, 1)
    run(pi)
    logo = set_logo(pi, 2)
    target = {"record": applier.RECORD, "first-picture": "splash.bin",
              "second-picture": "splash-45.bin"}[crash]
    write = applier.write_file

    def power_loss(directory, name, data):
        if name == target and os.fstat(directory).st_ino == pi.assets.joinpath(
                "default" if name == applier.RECORD else ".").stat().st_ino:
            raise OSError("simulated power loss")
        write(directory, name, data)

    monkeypatch.setattr(applier, "write_file", power_loss)
    assert run(pi)["state"] == "error"
    monkeypatch.setattr(applier, "write_file", write)
    (pi.assets / ".bosun-logo-splash-65.bin").write_bytes(b"left by a killed run")
    assert run(pi)["state"] == "applied"
    assert defaults(pi) == pi.pristine
    assert pictures(pi) == {name: composed(pi.pristine[name], logo) for name in applier.PICTURES}
    assert not (pi.assets / ".bosun-logo-splash-65.bin").exists()
    clear_logo(pi)
    run(pi)
    assert pictures(pi) == pi.pristine


@posix
def test_first_run_interrupted_before_its_record_starts_over(pi, monkeypatch):
    logo = set_logo(pi, 1)
    write = applier.write_file

    def power_loss(directory, name, data):
        if name == applier.RECORD:
            raise OSError("simulated power loss")
        write(directory, name, data)

    monkeypatch.setattr(applier, "write_file", power_loss)
    assert run(pi)["state"] == "error"
    assert pictures(pi) == pi.pristine
    monkeypatch.setattr(applier, "write_file", write)
    run(pi)
    assert defaults(pi) == pi.pristine
    assert pictures(pi) == {name: composed(pi.pristine[name], logo) for name in applier.PICTURES}


@posix
def test_changed_saved_default_is_an_error_not_a_guess(pi):
    set_logo(pi, 1)
    run(pi)
    before = pictures(pi)
    (pi.assets / "default/splash-80.bin").write_bytes(bsplash(640, 360, 99))
    status = run(pi)
    assert status["state"] == "error" and "install-boot-splash.sh" in status["error"]
    assert pictures(pi) == before


@posix
@pytest.mark.parametrize("layout", ["no-directory", "no-pictures"])
def test_missing_framebuffer_pictures_are_unavailable(pi, layout):
    set_logo(pi, 1)
    for name in applier.PICTURES:
        (pi.assets / name).unlink()
    if layout == "no-directory":
        pi.assets.rmdir()
    assert run(pi)["state"] == "unavailable"
    assert not (pi.assets / "default").exists()


@posix
@pytest.mark.parametrize("unsafe", ["group-writable", "world-writable", "default-writable", "owner"])
def test_unsafe_boot_splash_directories_are_refused(pi, monkeypatch, unsafe):
    if unsafe == "default-writable":
        run(pi)
        (pi.assets / "default").chmod(0o777)
    elif unsafe == "owner":
        uid = os.geteuid()
        monkeypatch.setattr(applier.os, "geteuid", lambda: uid + 1)
    else:
        pi.assets.chmod(0o775 if unsafe == "group-writable" else 0o757)
    set_logo(pi, 1)
    status = run(pi)
    assert status["state"] == "error" and "root-owned" in status["error"]
    assert pictures(pi) == pi.pristine


@posix
@pytest.mark.parametrize("kind", ["symlink", "oversize", "header", "fifo"])
def test_untrusted_hub_files_are_refused(pi, tmp_path, kind):
    logo = set_logo(pi, 1)
    target = pi.hub / "logo.bin"
    target.unlink()
    if kind == "symlink":
        (tmp_path / "elsewhere.bin").write_bytes(logo)
        target.symlink_to(tmp_path / "elsewhere.bin")
    elif kind == "oversize":
        target.write_bytes(logo + b"\0")
    elif kind == "header":
        target.write_bytes(b"BSPLASH1" + struct.pack("<II", 222, 400) + logo[16:])
    else:
        os.mkfifo(target)
    status = run(pi)
    assert status["state"] == "error"
    assert status["logo"] == (sha(target.read_bytes()) if kind == "header" else None)
    assert pictures(pi) == pi.pristine


@posix
def test_logo_replaced_during_a_run_is_applied_before_exit(pi, monkeypatch):
    set_logo(pi, 1)
    apply, replaced = applier.apply, []

    def apply_then_replace(root, logo):
        state = apply(root, logo)
        if not replaced:
            replaced.append(set_logo(pi, 3))  # its path event would be lost
        return state

    monkeypatch.setattr(applier, "apply", apply_then_replace)
    assert run(pi) == {"state": "applied", "logo": sha(replaced[0]), "error": None}
    assert pictures(pi) == {name: composed(pi.pristine[name], replaced[0]) for name in applier.PICTURES}


@posix
def test_matching_output_is_not_written_again(pi, monkeypatch):
    set_logo(pi, 1)
    run(pi)
    writes = []
    write = applier.write_file
    monkeypatch.setattr(applier, "write_file", lambda *args: (writes.append(args[1]), write(*args)))
    assert run(pi)["state"] == "applied"
    assert writes == []


@posix
def test_command_line_uses_root_and_reports_errors(pi):
    command = [sys.executable, "-I", str(SOURCE), "--root", str(pi.root)]
    logo = set_logo(pi, 1)
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    status = json.loads((pi.root / "run/bosun-boot-logo/status.json").read_text())
    assert status == {"state": "applied", "logo": sha(logo), "error": None}
    pi.assets.chmod(0o777)
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "root-owned" in result.stderr


@posix
def test_hub_and_applier_agree_on_the_early_status(pi):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bosun_hub.boot_logo import BootLogoService, PNG_SIGNATURE

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    stored_png = (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                  + chunk(b"IDAT", zlib.compress(bytes(5))) + chunk(b"IEND", b""))
    replies = []
    hub = SimpleNamespace(_offer_correlated=lambda sub, has_id, request_id, reply: replies.append(reply))
    unit = pi.root / "etc/systemd/system/bosun-boot-logo.path"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Path]\n")
    service = BootLogoService(hub, pi.hub, applier=unit, status=pi.root / "run/bosun-boot-logo/status.json")

    def send(kind, **args):
        service.handle(None, {"type": kind, **args})
        return replies.pop()

    logo = bsplash(400, 222, 7)
    data = stored_png + logo
    upload = send("HUB_BOOT_LOGO_BEGIN", png_size=len(stored_png), picture_size=len(logo),
                  sha256=sha(data))["upload"]
    for offset in range(0, len(data), 12288):
        send("HUB_BOOT_LOGO_CHUNK", upload=upload, offset=offset,
             data=base64.b64encode(data[offset:offset + 12288]).decode())
    assert send("HUB_BOOT_LOGO_COMMIT", upload=upload)["early"] == "pending"
    run(pi)
    assert send("HUB_BOOT_LOGO_INFO")["early"] == "applied"
    assert pictures(pi) == {name: composed(pi.pristine[name], logo) for name in applier.PICTURES}
    assert send("HUB_BOOT_LOGO_RESET")["early"] == "pending"
    run(pi)
    assert send("HUB_BOOT_LOGO_INFO") == {"type": "HUB_BOOT_LOGO", "supported": True, "custom": False,
                                          "early": "applied"}
    assert pictures(pi) == pi.pristine
