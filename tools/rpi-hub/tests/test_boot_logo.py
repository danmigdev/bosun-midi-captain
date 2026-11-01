"""Custom startup logo: hub protocol, storage boundaries and Stage page branding."""
import base64
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
import urllib.error
import urllib.request
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bosun_hub import boot_logo  # noqa: E402
from bosun_hub.boot_logo import BootLogoService, PICTURE_SIZE, brand_page  # noqa: E402
from bosun_hub.hub import Hub  # noqa: E402
from bosun_hub.server import _start_http  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


class Link:
    connected = True
    port_name = "/dev/ttyACM1"

    def __init__(self):
        self.sent = []

    def send(self, line):
        self.sent.append(line)
        return True


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(width=4, height=2):
    rows = b"".join(b"\0" + bytes(i % 256 for i in range(width * 4)) for _ in range(height))
    return (boot_logo.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def picture(seed=1):
    return b"BSPLASH1" + struct.pack("<II", 400, 222) + random.Random(seed).randbytes(400 * 222 * 3)


@pytest.fixture
def logo(tmp_path):
    hub = Hub(None)
    hub.link = Link()
    unit = tmp_path / "bosun-boot-logo.path"
    unit.write_text("[Path]\n")
    hub.boot_logo = BootLogoService(hub, tmp_path / "boot-logo", applier=unit,
                                    status=tmp_path / "run/status.json")
    hub.boot_logo.start()
    hub.sub = hub.subscribe()
    hub.dir = tmp_path / "boot-logo"
    return hub


def request(hub, kind, sub=None, **args):
    sub = sub or hub.sub
    sub.send(json.dumps({"type": kind, "id": "local-1", **args}))
    response = json.loads(sub._queue.get_nowait())
    assert response["id"] == "local-1"
    assert sub._queue.empty()
    return response


def begin(hub, data_png, data_picture, digest=None):
    return request(hub, "HUB_BOOT_LOGO_BEGIN", png_size=len(data_png), picture_size=len(data_picture),
                   sha256=digest or hashlib.sha256(data_png + data_picture).hexdigest())


def upload(hub, data_png=None, data_picture=None, digest=None):
    data_png = png() if data_png is None else data_png
    data_picture = picture() if data_picture is None else data_picture
    state = begin(hub, data_png, data_picture, digest)
    assert state == {"type": "HUB_BOOT_LOGO", "id": "local-1", "upload": state["upload"],
                     "received": 0, "size": len(data_png) + len(data_picture)}
    data = data_png + data_picture
    for offset in range(0, len(data), 12 * 1024):
        part = data[offset:offset + 12 * 1024]
        reply = request(hub, "HUB_BOOT_LOGO_CHUNK", upload=state["upload"], offset=offset,
                        data=base64.b64encode(part).decode())
        assert reply["received"] == offset + len(part)
    return state["upload"]


def trigger(hub):
    path = hub.dir / "apply"
    return path.read_text() if path.exists() else None


def test_upload_commit_stores_both_files_and_triggers_the_applier(logo):
    other = logo.subscribe()
    before = trigger(logo)
    job = upload(logo)
    reply = request(logo, "HUB_BOOT_LOGO_COMMIT", upload=job)
    assert reply == {"type": "HUB_BOOT_LOGO", "id": "local-1", "supported": True, "custom": True,
                     "early": "pending"}
    assert (logo.dir / "logo.png").read_bytes() == png()
    assert (logo.dir / "logo.bin").read_bytes() == picture()
    assert len(picture()) == PICTURE_SIZE == 266416
    assert trigger(logo) not in (None, before)
    info = request(logo, "HUB_BOOT_LOGO_INFO")
    assert info["custom"] is True and base64.b64decode(info["png"]) == png()
    # Pi-local: never forwarded to the Captain and private to the caller.
    assert logo.link.sent == []
    assert other._queue.empty()
    assert sorted(p.name for p in logo.dir.iterdir()) == ["apply", "logo.bin", "logo.png"]


def test_replacing_a_custom_logo_keeps_a_consistent_pair(logo):
    request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo))
    request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo, png(8, 8), picture(2)))
    assert (logo.dir / "logo.png").read_bytes() == png(8, 8)
    assert (logo.dir / "logo.bin").read_bytes() == picture(2)


def test_chunk_retry_is_idempotent_but_conflicts_and_gaps_fail(logo):
    data = png() + picture()
    state = begin(logo, png(), picture())
    first = base64.b64encode(data[:12288]).decode()
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=state["upload"], offset=0, data=first)["received"] == 12288
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=state["upload"], offset=0, data=first)["received"] == 12288
    changed = base64.b64encode(b"x" + data[1:12288]).decode()
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=state["upload"], offset=0, data=changed)["type"] == "ERROR"
    gap = base64.b64encode(data[12289:12300]).decode()
    out_of_order = request(logo, "HUB_BOOT_LOGO_CHUNK", upload=state["upload"], offset=12289, data=gap)
    assert out_of_order == {"type": "ERROR", "id": "local-1", "of": "HUB_BOOT_LOGO_CHUNK",
                            "error": "Boot logo chunk is out of order"}
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload="0" * 32, offset=12288, data=gap)["type"] == "ERROR"
    assert request(logo, "HUB_BOOT_LOGO_COMMIT", upload=state["upload"])["type"] == "ERROR"
    assert not (logo.dir / "logo.bin").exists()


def test_a_new_begin_replaces_the_unfinished_upload(logo):
    first = begin(logo, png(), picture())["upload"]
    second = upload(logo)
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=first, offset=0, data="AA==")["type"] == "ERROR"
    assert request(logo, "HUB_BOOT_LOGO_COMMIT", upload=second)["custom"] is True


def test_sha_mismatch_stores_nothing_and_drops_the_upload(logo):
    job = upload(logo, digest="0" * 64)
    reply = request(logo, "HUB_BOOT_LOGO_COMMIT", upload=job)
    assert reply["type"] == "ERROR" and "SHA-256" in reply["error"]
    assert request(logo, "HUB_BOOT_LOGO_COMMIT", upload=job)["type"] == "ERROR"
    assert request(logo, "HUB_BOOT_LOGO_INFO")["custom"] is False
    assert not (logo.dir / "logo.png").exists()


def corrupt_crc(data):
    return data[:29] + bytes([data[29] ^ 1]) + data[30:]


@pytest.mark.parametrize("bad_png", [
    b"\x89PNG\r\n\x1a\0" + png()[8:],  # signature
    boot_logo.PNG_SIGNATURE + chunk(b"IDAT", b"") + png()[8:],  # IHDR not first
    png(801, 1),
    png(1, 445),
    corrupt_crc(png()),
    png() + b"\0",  # data after IEND
    png()[:-12],  # no IEND
    png()[:-12] + chunk(b"IEND", b"x"),
], ids=["signature", "ihdr-not-first", "too-wide", "too-tall", "crc", "trailing-data", "no-iend", "iend-body"])
def test_malformed_png_is_rejected(logo, bad_png):
    reply = request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo, bad_png))
    assert reply["type"] == "ERROR" and "PNG" in reply["error"]
    assert not (logo.dir / "logo.png").exists() and not (logo.dir / "logo.bin").exists()


def test_largest_png_is_accepted(logo):
    assert request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo, png(800, 444)))["custom"] is True


@pytest.mark.parametrize("bad_picture", [
    b"BSPLASH2" + picture()[8:],
    b"BSPLASH1" + struct.pack("<II", 222, 400) + picture()[16:],
], ids=["magic", "dimensions"])
def test_malformed_boot_picture_is_rejected(logo, bad_picture):
    reply = request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo, data_picture=bad_picture))
    assert reply["type"] == "ERROR" and "BSPLASH1" in reply["error"]
    assert not (logo.dir / "logo.bin").exists()


@pytest.mark.parametrize("args", [
    {"png_size": 0}, {"png_size": 262145}, {"png_size": True}, {"png_size": "10"},
    {"picture_size": 266415}, {"picture_size": 266417}, {"sha256": "A" * 64}, {"sha256": None},
])
def test_begin_enforces_size_caps(logo, args):
    request_args = {"png_size": 262144, "picture_size": 266416, "sha256": "0" * 64, **args}
    assert request(logo, "HUB_BOOT_LOGO_BEGIN", **request_args)["type"] == "ERROR"


def test_chunk_size_and_bounds_are_enforced(logo):
    job = begin(logo, b"x" * 20000, picture())["upload"]
    too_large = base64.b64encode(b"x" * (12 * 1024 + 1)).decode()
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=job, offset=0, data=too_large)["type"] == "ERROR"
    for data in ("not base64!", "", None):
        assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=job, offset=0, data=data)["type"] == "ERROR"
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=job, offset=-1, data="AA==")["type"] == "ERROR"
    small = begin(logo, png(), picture())
    tail = base64.b64encode(b"x" * 2).decode()
    assert request(logo, "HUB_BOOT_LOGO_CHUNK", upload=small["upload"], offset=small["size"] - 1,
                   data=tail)["type"] == "ERROR"


def test_reset_removes_the_logo_and_triggers_the_applier(logo):
    request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo))
    before = trigger(logo)
    reply = request(logo, "HUB_BOOT_LOGO_RESET")
    assert reply == {"type": "HUB_BOOT_LOGO", "id": "local-1", "supported": True, "custom": False,
                     "early": "pending"}
    assert not (logo.dir / "logo.png").exists() and not (logo.dir / "logo.bin").exists()
    assert trigger(logo) != before
    assert request(logo, "HUB_BOOT_LOGO_RESET")["custom"] is False  # also when none is set


def test_info_without_a_custom_logo_has_no_png(logo):
    assert request(logo, "HUB_BOOT_LOGO_INFO") == {
        "type": "HUB_BOOT_LOGO", "id": "local-1", "supported": True, "custom": False, "early": "pending"}


def test_without_a_directory_the_hub_reports_unsupported(tmp_path):
    hub = Hub(None)
    hub.link = Link()
    hub.boot_logo.start()
    sub = hub.subscribe()
    hub.sub = sub
    assert request(hub, "HUB_BOOT_LOGO_INFO") == {
        "type": "HUB_BOOT_LOGO", "id": "local-1", "supported": False, "custom": False,
        "early": "unavailable"}
    for kind in ("HUB_BOOT_LOGO_BEGIN", "HUB_BOOT_LOGO_RESET", "HUB_BOOT_LOGO_COMMIT", "HUB_BOOT_LOGO_X"):
        assert request(hub, kind)["type"] == "ERROR"
    assert hub.link.sent == []


def test_requests_bypass_the_maintenance_gate_and_keep_correlation(logo):
    logo.updates.maintenance = True
    assert request(logo, "HUB_BOOT_LOGO_INFO")["type"] == "HUB_BOOT_LOGO"
    logo.sub.send(json.dumps({"type": "HUB_BOOT_LOGO_INFO"}))
    assert "id" not in json.loads(logo.sub._queue.get_nowait())
    assert request(logo, "GET_GLOBAL")["error"] == "maintenance_busy"


def write_status(hub, content):
    path = hub.boot_logo.status
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(content) if content else "{broken")


@pytest.mark.parametrize("custom, content, expected", [
    (False, None, ("pending", None)),
    (False, {"state": "default", "logo": None, "error": None}, ("applied", None)),
    (False, {"state": "applied", "logo": "f" * 64, "error": None}, ("pending", None)),
    (False, {"state": "default", "error": None}, ("pending", None)),
    (True, {"state": "applied", "logo": "@current", "error": None}, ("applied", None)),
    (True, {"state": "applied", "logo": "f" * 64, "error": None}, ("pending", None)),
    (True, {"state": "default", "logo": None, "error": None}, ("pending", None)),
    (True, {"state": "error", "logo": "@current", "error": "Refusing to write"}, ("error", "Refusing to write")),
    (True, {"state": "error", "logo": "f" * 64, "error": "Old failure"}, ("pending", None)),
    (True, {"state": "unavailable", "logo": "f" * 64, "error": None}, ("unavailable", None)),
    (True, {}, ("pending", None)),
])
def test_early_status_mapping(logo, custom, content, expected):
    if custom:
        request(logo, "HUB_BOOT_LOGO_COMMIT", upload=upload(logo))
    if content is not None:
        if content.get("logo") == "@current":
            content["logo"] = hashlib.sha256(picture()).hexdigest()
        write_status(logo, content)
    info = request(logo, "HUB_BOOT_LOGO_INFO")
    assert (info["early"], info.get("error")) == expected


def test_early_status_is_unavailable_without_the_applier(logo):
    logo.boot_logo.applier.unlink()
    write_status(logo, {"state": "default", "logo": None, "error": None})
    assert request(logo, "HUB_BOOT_LOGO_INFO")["early"] == "unavailable"


@pytest.mark.parametrize("leftover", ["logo.png", "logo.bin"])
def test_start_removes_a_half_stored_pair(tmp_path, leftover):
    directory = tmp_path / "boot-logo"
    directory.mkdir()
    (directory / leftover).write_bytes(b"partial")
    service = BootLogoService(None, directory, applier=tmp_path / "unit", status=tmp_path / "status.json")
    service.start()
    assert not (directory / leftover).exists()
    assert (directory / "apply").exists()  # the default pictures are re-checked too


def start_with_status(tmp_path, *, custom, content):
    directory = tmp_path / "boot-logo"
    directory.mkdir()
    if custom:
        (directory / "logo.png").write_bytes(png())
        (directory / "logo.bin").write_bytes(picture())
    (tmp_path / "unit").write_text("[Path]\n")
    (tmp_path / "status.json").write_text(json.dumps(content))
    service = BootLogoService(None, directory, applier=tmp_path / "unit", status=tmp_path / "status.json")
    service.start()
    return (directory / "apply").exists()


def test_start_always_reapplies_a_custom_logo(tmp_path):
    digest = hashlib.sha256(picture()).hexdigest()
    assert start_with_status(tmp_path, custom=True, content={"state": "applied", "logo": digest})


def test_start_leaves_matching_default_pictures_alone(tmp_path):
    assert not start_with_status(tmp_path, custom=False, content={"state": "default", "logo": None})


def test_start_reapplies_when_the_status_does_not_match(tmp_path):
    assert start_with_status(tmp_path, custom=False, content={"state": "applied", "logo": "f" * 64})


def test_start_creates_the_directory(tmp_path):
    BootLogoService(None, tmp_path / "missing/boot-logo", applier=tmp_path / "unit",
                    status=tmp_path / "status.json").start()
    assert (tmp_path / "missing/boot-logo/apply").exists()


# --------------------------------------------------------------------------
# Stage page branding
# --------------------------------------------------------------------------


def test_brand_page_replaces_the_icon_and_drops_the_name():
    template = (REPO / "editor/stage-kiosk.html").read_text(encoding="utf-8")
    page = brand_page(template, png())
    uri = "data:image/png;base64," + base64.b64encode(png()).decode()
    assert f'<img src="{uri}" alt="" style="width:auto;height:auto;max-width:min(90vw,400px);' \
           f'max-height:min(50vh,222px)" />' in page
    assert "__BOSUN_BOOT_LOGO__" not in page
    assert "<strong>Bosun</strong>" not in page
    assert "<small>v__BOSUN_VERSION__</small>" in page
    assert page.count("<img") == template.count("<img") == 1
    # Everything outside the splash block is byte-for-byte unchanged.
    start = template.index("<!-- BOSUN BOOT SPLASH BEGIN -->")
    end = template.index("<!-- BOSUN BOOT SPLASH END -->")
    assert page.startswith(template[:start])
    assert page.endswith(template[end:])


def test_brand_page_needs_the_splash_block():
    assert brand_page("<html><body><img src='x'><strong>Bosun</strong></body></html>", png()) is None


@pytest.fixture
def stage(tmp_path):
    directory = tmp_path / "stage"
    directory.mkdir()
    service = BootLogoService(None, tmp_path / "boot-logo", applier=tmp_path / "unit",
                              status=tmp_path / "status.json")
    service.start()
    httpd = _start_http(directory, "127.0.0.1", 0, service)

    def get(path, method="GET"):
        url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as response:
            return response.headers, response.read()

    yield directory, service, get
    httpd.shutdown()
    httpd.server_close()


def test_http_serves_the_custom_logo_without_rewriting_the_bundle(stage):
    directory, service, get = stage
    template = (REPO / "editor/stage-kiosk.html").read_bytes()
    (directory / "index.html").write_bytes(template)
    (directory / "app.js").write_text("<strong>Bosun</strong>")
    service.directory.joinpath("logo.png").write_bytes(png())
    service.directory.joinpath("logo.bin").write_bytes(picture())
    for path in ("/", "/index.html", "/index.html?reload=1"):
        headers, body = get(path)
        assert headers["Cache-Control"] == "no-store"
        assert int(headers["Content-Length"]) == len(body)
        assert body.decode() == brand_page(template.decode(), png())
    headers, body = get("/", method="HEAD")
    assert body == b"" and int(headers["Content-Length"]) == len(brand_page(template.decode(), png()).encode())
    assert get("/app.js")[1] == b"<strong>Bosun</strong>"
    assert (directory / "index.html").read_bytes() == template


def test_http_serves_the_page_unchanged_by_default_or_without_the_block(stage):
    directory, service, get = stage
    template = (REPO / "editor/stage-kiosk.html").read_bytes()
    (directory / "index.html").write_bytes(template)
    headers, body = get("/")
    assert body == template and headers["Cache-Control"] == "no-store"
    # Only logo.png, as a crash could leave it, is not a custom logo.
    service.directory.joinpath("logo.png").write_bytes(png())
    assert get("/index.html")[1] == template
    service.directory.joinpath("logo.bin").write_bytes(picture())
    (directory / "index.html").write_bytes(b"<h1>stage kiosk</h1>")
    assert get("/")[1] == b"<h1>stage kiosk</h1>"



def test_http_serves_the_custom_png_for_the_update_screen(stage):
    directory, service, get = stage
    def missing(path="/boot-logo.png"):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(path)
        assert error.value.code == 404 and error.value.headers["Cache-Control"] == "no-store"
        error.value.close()
    missing()  # Stage falls back to its bundled Bosun icon
    service.directory.joinpath("logo.png").write_bytes(png())
    missing()  # only logo.png, as a crash could leave it, is not a custom logo
    service.directory.joinpath("logo.bin").write_bytes(picture())
    for path in ("/boot-logo.png", "/boot-logo.png?t=1"):
        headers, body = get(path)
        assert body == png()
        assert headers["Content-Type"] == "image/png" and headers["Cache-Control"] == "no-store"
        assert int(headers["Content-Length"]) == len(body)
    headers, body = get("/boot-logo.png", method="HEAD")
    assert body == b"" and int(headers["Content-Length"]) == len(png())
    # The store decides, never a same-named file in the bundle.
    (directory / "boot-logo.png").write_bytes(b"bundle")
    service.directory.joinpath("logo.bin").unlink()
    missing()


def test_http_without_a_logo_store_has_no_boot_logo(tmp_path):
    httpd = _start_http(tmp_path, "127.0.0.1", 0, None)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/boot-logo.png", timeout=5)
        assert error.value.code == 404
        error.value.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_serves_the_configured_store_over_tcp_and_http(tmp_path, monkeypatch):
    import asyncio
    from bosun_hub import server
    from tests.fake_pedal import FakePedal

    stage = tmp_path / "stage"
    stage.mkdir()
    template = (REPO / "editor/stage-kiosk.html").read_bytes()
    (stage / "index.html").write_bytes(template)
    store = tmp_path / "boot-logo"
    store.mkdir()
    (store / "logo.png").write_bytes(png())
    (store / "logo.bin").write_bytes(picture())
    started = []
    start_http = server._start_http
    monkeypatch.setattr(server, "_start_http", lambda *args: started.append(start_http(*args)) or started[-1])

    async def body():
        ready = asyncio.get_running_loop().create_future()

        async def discovery_stub(host, tcp_port):
            ready.set_result(tcp_port)

        monkeypatch.setattr(server, "serve_discovery", discovery_stub)
        pedal = FakePedal()
        task = asyncio.create_task(server.run(
            f"tcp://127.0.0.1:{pedal.port}", host="127.0.0.1", tcp_port=0, ws_port=0, http_port=0,
            stage_dir=stage, boot_logo_dir=store))
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", await asyncio.wait_for(ready, 3))
            writer.write(b'{"type":"HUB_BOOT_LOGO_INFO","id":"logo-1"}\n')
            await writer.drain()
            while (reply := json.loads(await asyncio.wait_for(reader.readline(), 3))).get("id") != "logo-1":
                pass
            assert reply["supported"] is True and base64.b64decode(reply["png"]) == png()
            url = f"http://127.0.0.1:{started[0].server_address[1]}/"
            page = await asyncio.get_running_loop().run_in_executor(
                None, lambda: urllib.request.urlopen(url, timeout=5).read())
            assert page.decode() == brand_page(template.decode(), png())
            assert (store / "apply").exists()  # start() asked the applier to re-check
        finally:
            if writer is not None:
                writer.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pedal.close()

    asyncio.run(body())
