"""Cold Captain OTA handlers; imported only while a file upload is active."""

import binascii
import gc
import os


def _mkdir_p(path):
    parts = [p for p in path.strip("/").split("/") if p]
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            pass



def begin(self, mid, msg):
    path = msg.get("path", "")
    if not path or not path.startswith("/"):
        self._send({"type": "ERROR", "id": mid, "error": "bad_path"})
        return
    expected_size = msg.get("size")
    if (expected_size is not None and
            (not isinstance(expected_size, int) or
             isinstance(expected_size, bool) or expected_size < 0)):
        self._send({"type": "ERROR", "id": mid, "error": "bad_size"})
        return
    # A filesystem write is an auto-reload trigger in CircuitPython.  The
    # first PUT_FILE_CHUNK used to interrupt code.py before its ACK could
    # be sent, leaving the board rebooting with only a partial .tmp file.
    # Disable reload *before* even opening/truncating that file.  It stays
    # disabled for the rest of this VM run: a normal firmware transaction
    # can contain several files and finishes with an explicit REBOOT.
    try:
        import supervisor
        supervisor.runtime.autoreload = False
    except Exception as e:
        # Fail closed.  Proceeding would advertise a successful BEGIN for
        # a transaction CircuitPython is free to kill on its first write.
        self._send({"type": "ERROR", "id": mid,
                    "error": "autoreload", "detail": str(e)})
        return
    parent = path.rsplit("/", 1)[0]
    if parent:
        try:
            _mkdir_p(parent)
        except OSError as e:
            self._send({"type": "ERROR", "id": mid, "error": "mkdir", "detail": str(e)})
            return
    old = self._uploads.pop(path, None)
    self._upload_sizes.pop(path, None)
    if old is not None:
        try: old.close()
        except Exception: pass
    try:
        f = open(path + ".tmp", "wb")
    except OSError as e:
        self._send({"type": "ERROR", "id": mid, "error": "open", "detail": str(e)})
        return
    self._uploads[path] = f
    self._upload_sizes[path] = expected_size
    # Negotiate the integrity guarantee explicitly. Older Captain builds
    # accept the unknown `size` request field but do not validate it at
    # END; a client must not treat an uncertain chunk ACK as safe unless
    # this exact capability/value comes back from BEGIN.
    ack = {"type": "ACK", "id": mid,
           "size_check": expected_size is not None}
    if expected_size is not None:
        ack["size"] = expected_size
    self._send(ack)

    return True


def chunk(self, mid, msg):
    path = msg.get("path", "")
    f = self._uploads.get(path)
    if f is None:
        self._send({"type": "ERROR", "id": mid, "error": "no_open_file"})
        return
    data = None
    try:
        data = binascii.a2b_base64(msg.get("data_b64", ""))
        # The base64 string is by far the largest member of the inbound
        # command.  poll()'s caller retains `msg` until the whole main-loop
        # tick ends, so drop it as soon as it is decoded.  Otherwise the
        # encoded and decoded chunks overlap with the subsequent
        # usb_midi.read(256), which can push the fragmented RP2040 heap
        # over the edge (observed as allocation failures of 257 bytes).
        try:
            del msg["data_b64"]
        except Exception:
            pass
        f.write(data)
        data = None
        gc.collect()
    except Exception as e:
        # A failed chunk invalidates the transaction.  Release the open
        # file immediately rather than pinning its FAT/file buffers while
        # the host waits for a timeout and restarts from BEGIN.
        data = None
        try:
            del msg["data_b64"]
        except Exception:
            pass
        self._uploads.pop(path, None)
        self._upload_sizes.pop(path, None)
        try:
            f.close()
        except Exception:
            pass
        try:
            gc.collect()
        except Exception:
            pass
        self._send({"type": "ERROR", "id": mid, "error": "write", "detail": str(e)})
        return
    self._send({"type": "ACK", "id": mid})

    return True


def end(self, mid, msg):
    path = msg.get("path", "")
    f = self._uploads.pop(path, None)
    expected_size = self._upload_sizes.pop(path, None)
    if f is None:
        self._send({"type": "ERROR", "id": mid, "error": "no_open_file"})
        return
    try:
        f.close()
        if expected_size is not None:
            actual_size = os.stat(path + ".tmp")[6]
            if actual_size != expected_size:
                try:
                    os.remove(path + ".tmp")
                except OSError:
                    pass
                self._send({"type": "ERROR", "id": mid,
                            "error": "size_mismatch",
                            "expected": expected_size,
                            "actual": actual_size})
                return
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(path + ".tmp", path)
        # CircuitPython imports a source sibling in preference to its
        # compiled form.  A historical full deploy could therefore leave
        # an old .py shadowing the freshly installed .mpy forever.  Prune
        # only that exact derived sibling, and only after the verified
        # temporary file has replaced the live compiled module.
        if path.endswith(".mpy"):
            source_path = path[:-4] + ".py"
            try:
                os.remove(source_path)
            except OSError:
                pass
            try:
                os.stat(source_path)
            except OSError:
                pass
            else:
                self._send({"type": "ERROR", "id": mid,
                            "error": "source_shadow",
                            "path": source_path})
                return
    except OSError as e:
        self._send({"type": "ERROR", "id": mid, "error": "rename", "detail": str(e)})
        return
    self._send({"type": "ACK", "id": mid})

    return True
