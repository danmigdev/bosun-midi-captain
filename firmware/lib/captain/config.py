"""Configuration loader, profile-scoped.

Storage layout:

    /config/
      active_profile.json          # {"id": "<profile_id>"} - absent until a profile is created
      profiles/
        <profile_id>/
          manifest.json            # {name, kind, color?}
          device.json
          midi_learn.json
          patches/<bb>/<ss>.json

There is no default profile. A fresh install has no active profile and
no profiles directory; the editor prompts the user to create one before
patches can be edited.
"""
import json
import os


CONFIG_ROOT = "/config"


# ---------- low-level path helpers ----------

def _join(*parts):
    return "/".join(p.rstrip("/") for p in parts if p)


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _isdir(path):
    try:
        return (os.stat(path)[0] & 0x4000) != 0
    except OSError:
        return False


def _mkdir_p(path):
    parts = [p for p in path.strip("/").split("/") if p]
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return default
    except ValueError:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


# ---------- profile management ----------

def active_profile_id():
    """Returns the active profile id, or "" if none is set / valid."""
    obj = _read_json(_join(CONFIG_ROOT, "active_profile.json"), default={})
    pid = obj.get("id", "")
    if not pid:
        return ""
    if not _isdir(_join(CONFIG_ROOT, "profiles", pid)):
        return ""
    return pid


def set_active_profile_id(pid):
    if not _isdir(_join(CONFIG_ROOT, "profiles", pid)):
        raise ValueError("no such profile: " + pid)
    _write_json(_join(CONFIG_ROOT, "active_profile.json"), {"id": pid})


def clear_active_profile():
    """Forget the active-profile pointer (used when the last profile is deleted)."""
    path = _join(CONFIG_ROOT, "active_profile.json")
    try:
        os.remove(path)
    except OSError:
        pass


def list_profiles():
    """Returns list of {id, name, kind, active}."""
    profiles_dir = _join(CONFIG_ROOT, "profiles")
    out = []
    active = active_profile_id()
    try:
        ids = os.listdir(profiles_dir)
    except OSError:
        return out
    for pid in sorted(ids):
        if not _isdir(_join(profiles_dir, pid)):
            continue
        manifest = _read_json(_join(profiles_dir, pid, "manifest.json"),
                              default={"name": pid, "kind": "unknown"})
        out.append({
            "id": pid,
            "name": manifest.get("name", pid),
            "kind": manifest.get("kind", "unknown"),
            "color": manifest.get("color"),
            "active": pid == active,
        })
    return out


def create_profile(pid, name, kind, default_layout=None, color=None):
    """Create an empty profile. Raises ValueError if id is invalid or exists.

    `default_layout`, if given, is the tft.layout to seed the new profile's
    device.json with - usually pulled from the matching plugin's
    DEFAULT_LAYOUT so the user gets a sensible TFT out of the box.
    `color`, if given, is an optional "#rrggbb" accent stored in the
    manifest and surfaced by the editor's profile picker."""
    if not pid or not _valid_id(pid):
        raise ValueError("invalid profile id (use a-z 0-9 -)")
    profile_dir = _join(CONFIG_ROOT, "profiles", pid)
    if _isdir(profile_dir):
        raise ValueError("profile already exists: " + pid)
    _mkdir_p(_join(profile_dir, "patches"))
    manifest = {
        "name": name or pid,
        "kind": kind or "unknown",
    }
    if color:
        manifest["color"] = color
    _write_json(_join(profile_dir, "manifest.json"), manifest)
    dev = _empty_device_json()
    if default_layout:
        dev["tft"]["layout"] = default_layout
    _write_json(_join(profile_dir, "device.json"), dev)
    _write_json(_join(profile_dir, "midi_learn.json"), {"pc_to_patch": []})
    # First profile created becomes active automatically.
    if not active_profile_id():
        set_active_profile_id(pid)


def delete_profile(pid):
    """Remove a profile. Clears the active pointer if it was the active one."""
    profile_dir = _join(CONFIG_ROOT, "profiles", pid)
    if not _isdir(profile_dir):
        raise ValueError("no such profile: " + pid)
    was_active = (pid == active_profile_id())
    _rmtree(profile_dir)
    if was_active:
        clear_active_profile()


def rename_profile(pid, new_name):
    manifest_path = _join(CONFIG_ROOT, "profiles", pid, "manifest.json")
    if not _exists(manifest_path):
        raise ValueError("no such profile: " + pid)
    m = _read_json(manifest_path, default={})
    m["name"] = new_name
    _write_json(manifest_path, m)


def _valid_id(pid):
    if len(pid) > 32:
        return False
    for c in pid:
        if not (c.isalpha() or c.isdigit() or c == "-" or c == "_"):
            return False
    return True


def _rmtree(path):
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        sub = _join(path, name)
        if _isdir(sub):
            _rmtree(sub)
        else:
            try:
                os.remove(sub)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _empty_device_json():
    """Minimal device.json template for a freshly-created profile."""
    return {
        "version": 1,
        "long_press_ms": 600,
        "double_tap_window_ms": 250,
        "auto_momentary_on_hold": True,
        "auto_momentary_ms": 500,
        "long_press_actions": {},
        "autosave": {"enabled": False, "debounce_ms": 2000},
        "leds": {"brightness": 64},
        "tft": {"brightness": 80, "theme_color": "#00ff88", "rotation": 180, "rowstart": 80, "colstart": 0},
        "expression": [
            {"jack": 1, "enabled": False, "invert": False,
             "calibration": {"min": 300, "max": 65200}, "curve": "linear",
             "message": {"type": "cc", "channel": 1, "cc": 11, "value": 0}},
            {"jack": 2, "enabled": False, "invert": False,
             "calibration": {"min": 300, "max": 65200}, "curve": "linear",
             "message": {"type": "cc", "channel": 1, "cc": 11, "value": 0}},
        ],
    }


# ---------- profile-scoped path helpers ----------

def _profile_root(profile=None):
    pid = profile or active_profile_id()
    if not pid:
        # No active profile - return a sentinel path that doesn't exist so
        # downstream open()/mkdir() calls fail with OSError. Caller code
        # should generally check active_profile_id() first.
        return _join(CONFIG_ROOT, "profiles", "__none__")
    return _join(CONFIG_ROOT, "profiles", pid)


def patch_path(bank, slot, profile=None):
    return _join(_profile_root(profile),
                 "patches",
                 "{:02d}".format(bank),
                 "{:02d}.json".format(slot))


def device_path(profile=None):
    return _join(_profile_root(profile), "device.json")


def midi_learn_path(profile=None):
    return _join(_profile_root(profile), "midi_learn.json")


# ---------- public load/save API (profile-scoped) ----------

def load_device():
    """Returns the active profile's device.json, or in-memory defaults
    if no profile is active. Does NOT persist defaults - they're transient
    until the user creates a profile."""
    if not active_profile_id():
        return _empty_device_json()
    return _read_json(device_path(), default=_empty_device_json())


def load_midi_learn():
    if not active_profile_id():
        return {"pc_to_patch": []}
    return _read_json(midi_learn_path(), default={"pc_to_patch": []})


def load_patch(bank, slot):
    with open(patch_path(bank, slot)) as f:
        return json.load(f)


def save_patch(bank, slot, patch):
    bank_dir = _join(_profile_root(), "patches", "{:02d}".format(bank))
    _mkdir_p(bank_dir)
    _write_json(patch_path(bank, slot), patch)


def save_device(device):
    _mkdir_p(_profile_root())
    _write_json(device_path(), device)


def save_midi_learn(table):
    _mkdir_p(_profile_root())
    _write_json(midi_learn_path(), table)


def list_patches(profile=None):
    """Returns a list of (bank, slot) tuples for the given profile.
    Defaults to the active one; pass an explicit profile id to read
    another profile's patches without touching the active state -
    used by the editor's cross-profile export to avoid a reboot per
    profile."""
    if profile is None and not active_profile_id():
        return []
    banks_dir = _join(_profile_root(profile), "patches")
    out = []
    try:
        bank_names = sorted(os.listdir(banks_dir))
    except OSError:
        return out
    for bank_name in bank_names:
        bank_path = _join(banks_dir, bank_name)
        try:
            entries = os.listdir(bank_path)
        except OSError:
            continue
        for slot_name in sorted(entries):
            if slot_name.endswith(".json") and not slot_name.endswith(".tmp"):
                try:
                    out.append((int(bank_name), int(slot_name[:-5])))
                except ValueError:
                    pass
    return out


def load_device_for(profile):
    """Same as load_device() but for an arbitrary profile - no active
    profile switching required. Returns empty defaults if the profile
    has no device.json yet."""
    return _read_json(device_path(profile), default=_empty_device_json())


def load_patch_for(bank, slot, profile):
    """Same as load_patch() but for an arbitrary profile. Raises OSError
    if the patch file doesn't exist (caller treats as 'not_found')."""
    with open(patch_path(bank, slot, profile)) as f:
        return json.load(f)


def load_midi_learn_for(profile):
    """Same as load_midi_learn() but for an arbitrary profile."""
    return _read_json(midi_learn_path(profile), default={"pc_to_patch": []})


def profile_exists(profile):
    """True if a profile with this id exists on disk. Used by the
    protocol layer to validate cross-profile read requests before
    surfacing a file-not-found OSError."""
    return _isdir(_join(CONFIG_ROOT, "profiles", profile))


def save_device_for(device, profile):
    """Cross-profile write of device.json - lands on disk for the named
    profile without touching the active in-memory state. Used by the
    editor's bulk import flow so creating N profiles from backups
    doesn't trigger N reboots."""
    _mkdir_p(_profile_root(profile))
    _write_json(device_path(profile), device)


def save_patch_for(bank, slot, patch, profile):
    """Cross-profile write of a single patch."""
    bank_dir = _join(_profile_root(profile), "patches", "{:02d}".format(bank))
    _mkdir_p(bank_dir)
    _write_json(patch_path(bank, slot, profile), patch)


def save_midi_learn_for(table, profile):
    """Cross-profile write of midi_learn.json."""
    _mkdir_p(_profile_root(profile))
    _write_json(midi_learn_path(profile), table)
