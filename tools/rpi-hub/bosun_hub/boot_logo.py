"""Custom startup logo for the Pi boot screens (``HUB_BOOT_LOGO_*``).

The editor renders both formats; the hub only checks their structure and
stores them in its own state directory. Stage's loading screen gets
``logo.png`` when the HTTP server sends ``index.html``. The root-only
``bosun-boot-logo.path`` pastes ``logo.bin`` into the early framebuffer
pictures whenever the ``apply`` trigger changes. Nothing here reaches the
Captain, so these requests are answered even during maintenance.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import struct
import zlib

MAX_PNG = 256 * 1024
PNG_LIMIT = (800, 444)
PICTURE = (400, 222)
PICTURE_SIZE = 16 + PICTURE[0] * PICTURE[1] * 3
MAX_CHUNK = 12 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
APPLIER = Path("/etc/systemd/system/bosun-boot-logo.path")
STATUS = Path("/run/bosun-boot-logo/status.json")
SPLASH = re.compile(r"<!-- BOSUN BOOT SPLASH BEGIN -->.*?<!-- BOSUN BOOT SPLASH END -->", re.DOTALL)
IMAGE_STYLE = "width:auto;height:auto;max-width:min(90vw,400px);max-height:min(50vh,222px)"
log = logging.getLogger("bosun_hub.boot_logo")


def check_png(data: bytes) -> None:
    """Check the chunk structure only; Chromium decodes the pixels."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("The Stage logo is not a PNG")
    offset, first = len(PNG_SIGNATURE), True
    while True:
        if offset + 12 > len(data):
            raise ValueError("The Stage logo PNG is truncated")
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("The Stage logo PNG is truncated")
        if zlib.crc32(data[offset + 4:end - 4]) != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError("The Stage logo PNG is corrupted")
        if first:
            if kind != b"IHDR" or length != 13:
                raise ValueError("The Stage logo PNG must start with its header")
            width, height = struct.unpack(">II", data[offset + 8:offset + 16])
            if not (0 < width <= PNG_LIMIT[0] and 0 < height <= PNG_LIMIT[1]):
                raise ValueError("The Stage logo PNG exceeds 800x444 pixels")
            first = False
        offset = end
        if kind == b"IEND":
            if length or offset != len(data):
                raise ValueError("The Stage logo PNG has data after its end")
            return


def check_picture(data: bytes) -> None:
    if (len(data) != PICTURE_SIZE or data[:8] != b"BSPLASH1"
            or struct.unpack("<II", data[8:16]) != PICTURE):
        raise ValueError("The boot picture logo must be a 400x222 BSPLASH1 image")


def brand_page(html: str, png: bytes) -> str | None:
    """Stage's index.html with the custom logo in place of the icon and name."""
    block = SPLASH.search(html)
    image = block and re.search(r"<img\b[^>]*>", block.group())
    if not image:
        return None
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    markup = block.group()
    markup = (markup[:image.start()] + f'<img src="{uri}" alt="" style="{IMAGE_STYLE}" />'
              + markup[image.end():]).replace("<strong>Bosun</strong>", "", 1)
    return html[:block.start()] + markup + html[block.end():]


def _sync_directory(directory: Path) -> None:
    # Windows hosts run the protocol tests but cannot fsync a directory.
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    _sync_directory(path.parent)


class BootLogoService:
    def __init__(self, hub, directory=None, *, applier=APPLIER, status=STATUS):
        self.hub = hub
        self.directory = Path(directory) if directory is not None else None
        self.applier = Path(applier)
        self.status = Path(status)
        self._upload = None

    def _paths(self):
        return self.directory / "logo.png", self.directory / "logo.bin"

    def custom_png(self) -> bytes | None:
        """The stored PNG while a custom logo is set; safe from the HTTP thread."""
        if self.directory is None:
            return None
        png, picture = self._paths()
        try:
            return png.read_bytes() if picture.is_file() else None
        except OSError:
            return None

    def _logo_digest(self):
        """SHA-256 of logo.bin while both files exist, else None (Bosun logo)."""
        png, picture = self._paths()
        if not png.is_file():
            return None
        try:
            return hashlib.sha256(picture.read_bytes()).hexdigest()
        except FileNotFoundError:
            return None

    def _early(self, digest):
        if not self.applier.exists():
            return "unavailable", None
        try:
            status = json.loads(self.status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = None
        if not isinstance(status, dict):
            return "pending", None
        state = status.get("state")
        if state == "unavailable":
            return "unavailable", None
        if status.get("logo", "") != digest:
            return "pending", None
        if state == ("applied" if digest else "default"):
            return "applied", None
        if state == "error":
            return "error", str(status.get("error") or "The boot pictures could not be updated")
        return "pending", None

    def info(self, *, png=True):
        if self.directory is None:
            return {"type": "HUB_BOOT_LOGO", "supported": False, "custom": False,
                    "early": "unavailable"}
        digest = self._logo_digest()
        early, error = self._early(digest)
        reply = {"type": "HUB_BOOT_LOGO", "supported": True, "custom": digest is not None,
                 "early": early}
        if error:
            reply["error"] = error
        if png and digest is not None:
            reply["png"] = base64.b64encode(self._paths()[0].read_bytes()).decode("ascii")
        return reply

    def start(self):
        """Drop a half-stored pair, then have the applier re-check the pictures."""
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            png, picture = self._paths()
            if png.exists() != picture.exists():
                picture.unlink(missing_ok=True)
                png.unlink(missing_ok=True)
                _sync_directory(self.directory)
            digest = self._logo_digest()
            # A reboot empties /run, and an installer refresh or a trial commit
            # may have put Bosun pictures back: re-apply rather than trust them.
            if digest is not None or self._early(digest)[0] != "applied":
                self._trigger()
        except OSError:
            log.exception("Cannot prepare the custom startup logo in %s", self.directory)

    def _trigger(self):
        # Rewritten in place: closing the written file is the change
        # PathChanged= reacts to. The applier never reads the nonce.
        with (self.directory / "apply").open("w", encoding="ascii") as out:
            out.write(secrets.token_hex(16) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def handle(self, sub, request):
        kind = request.get("type", "")
        if not isinstance(kind, str) or not kind.startswith("HUB_BOOT_LOGO_"):
            return False
        try:
            if kind == "HUB_BOOT_LOGO_INFO":
                reply = self.info()
            elif self.directory is None:
                raise ValueError("This hub cannot store a startup logo")
            elif kind == "HUB_BOOT_LOGO_BEGIN":
                reply = self._begin(request)
            elif kind == "HUB_BOOT_LOGO_CHUNK":
                reply = self._chunk(request)
            elif kind == "HUB_BOOT_LOGO_COMMIT":
                reply = self._commit(request)
            elif kind == "HUB_BOOT_LOGO_RESET":
                reply = self._reset()
            else:
                raise ValueError("Unknown boot logo command")
        except (OSError, ValueError, TypeError) as error:
            reply = {"type": "ERROR", "of": kind, "error": str(error)}
        self.hub._offer_correlated(sub, "id" in request, request.get("id"), reply)
        return True

    def _progress(self, upload):
        return {"type": "HUB_BOOT_LOGO", "upload": upload["upload"],
                "received": len(upload["data"]), "size": upload["size"]}

    def _current(self, request):
        upload = self._upload
        if upload is None or request.get("upload") != upload["upload"]:
            raise ValueError("Unknown boot logo upload; start again")
        return upload

    def _begin(self, request):
        png_size, picture_size = request.get("png_size"), request.get("picture_size")
        digest = request.get("sha256")
        if type(png_size) is not int or not 0 < png_size <= MAX_PNG:
            raise ValueError(f"The Stage logo PNG must be 1 to {MAX_PNG} bytes")
        if type(picture_size) is not int or picture_size != PICTURE_SIZE:
            raise ValueError(f"The boot picture logo must be {PICTURE_SIZE} bytes")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid boot logo SHA-256")
        # One upload at a time; a new BEGIN abandons any unfinished one.
        self._upload = {"upload": secrets.token_hex(16), "png_size": png_size, "sha256": digest,
                        "size": png_size + picture_size, "data": bytearray()}
        return self._progress(self._upload)

    def _chunk(self, request):
        upload = self._current(request)
        offset, encoded = request.get("offset"), request.get("data")
        if (type(offset) is not int or offset < 0 or not isinstance(encoded, str)
                or len(encoded) > 4 * ((MAX_CHUNK + 2) // 3)):
            raise ValueError("Invalid boot logo chunk")
        data = base64.b64decode(encoded, validate=True)
        received = upload["data"]
        if not data or len(data) > MAX_CHUNK or offset + len(data) > upload["size"]:
            raise ValueError("Boot logo chunk exceeds the upload bounds")
        if offset < len(received):
            # A lost reply can safely retry exactly the same bytes.
            if offset + len(data) > len(received) or received[offset:offset + len(data)] != data:
                raise ValueError("Conflicting boot logo chunk retry")
        elif offset != len(received):
            raise ValueError("Boot logo chunk is out of order")
        else:
            received += data
        return self._progress(upload)

    def _commit(self, request):
        upload = self._current(request)
        self._upload = None
        data = bytes(upload["data"])
        if len(data) != upload["size"]:
            raise ValueError("Boot logo upload is incomplete; start again")
        if hashlib.sha256(data).hexdigest() != upload["sha256"]:
            raise ValueError("Boot logo upload failed its SHA-256 check")
        png, picture = data[:upload["png_size"]], data[upload["png_size"]:]
        check_png(png)
        check_picture(picture)
        png_path, picture_path = self._paths()
        try:
            # logo.bin completes the pair: a crash leaves at most logo.png,
            # which start() removes, never a mismatched pair.
            picture_path.unlink(missing_ok=True)
            _write(png_path, png)
            _write(picture_path, picture)
        finally:
            self._trigger()
        return self.info(png=False)

    def _reset(self):
        png_path, picture_path = self._paths()
        try:
            picture_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)
            _sync_directory(self.directory)
        finally:
            self._trigger()
        return self.info(png=False)
