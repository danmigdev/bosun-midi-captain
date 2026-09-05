#!/usr/bin/env python3
"""Focused tests for PatchStore's bounded, metadata-only inventory reads.

LIST_PATCHES and preset-navigation discovery are frequent read paths on the
Captain.  Neither may retain every complete patch (including all bindings) in
the store cache merely to answer existence or metadata questions.
"""

import errno
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys


FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

from captain import config  # noqa: E402
from captain.store import PatchStore  # noqa: E402


class PatchStoreMetadataTests(unittest.TestCase):
    def setUp(self):
        self._originals = {
            "list_patches": config.list_patches,
            "load_patch": config.load_patch,
            "patch_path": config.patch_path,
            "save_patch": config.save_patch,
        }

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(config, name, value)

    def test_many_disk_patches_never_grow_complete_patch_cache(self):
        disk_ids = [(bank, slot)
                    for bank in range(1, 17)
                    for slot in range(1, 9)]
        load_calls = []

        config.list_patches = lambda: list(reversed(disk_ids))

        def load_patch(bank, slot):
            load_calls.append((bank, slot))
            return {
                "name": "Rig %02d-%02d" % (bank, slot),
                "linked_to": ({"bank": bank, "slot": slot - 1}
                              if slot > 1 else None),
                # Model the memory-heavy portion LIST_PATCHES must not retain.
                "bindings": [{"switch": str(i), "payload": "x" * 128}
                             for i in range(10)],
            }

        config.load_patch = load_patch
        patches = PatchStore()

        first = patches.list()
        second = patches.list()

        self.assertEqual(128, len(first))
        self.assertEqual(first, second)
        self.assertEqual(disk_ids, [(p["bank"], p["slot"]) for p in first])
        self.assertEqual("Rig 01-01", first[0]["name"])
        self.assertEqual({"bank": 16, "slot": 7},
                         first[-1]["linked_to"])
        self.assertEqual(256, len(load_calls))
        self.assertEqual({}, patches._cache,
                         "inventory retained complete patches in RAM")

    def test_only_one_transient_complete_patch_is_live_at_a_time(self):
        alive = [0]

        class TrackedPatch:
            def __init__(self, slot):
                alive[0] += 1
                self.name = "Rig %d" % slot
                self.bindings = ["x" * 256 for _ in range(20)]

            def get(self, key, default=None):
                if key == "name":
                    return self.name
                return default

            def __del__(self):
                alive[0] -= 1

        config.list_patches = lambda: [(1, slot) for slot in range(1, 17)]

        def load_patch(_bank, slot):
            # CPython's reference counting makes this a deterministic guard
            # that the preceding full graph was unrooted before this call.
            self.assertEqual(0, alive[0],
                             "two complete transient patches overlap")
            return TrackedPatch(slot)

        config.load_patch = load_patch
        patches = PatchStore()

        got = patches.list()

        self.assertEqual(0, alive[0])
        self.assertEqual(["Rig %d" % slot for slot in range(1, 17)],
                         [entry["name"] for entry in got])
        self.assertEqual({}, patches._cache)

    def test_cached_dirty_and_ram_only_values_take_precedence(self):
        disk = {
            (1, 1): {"name": "Cached original", "linked_to": {"bank": 9, "slot": 1}},
            (1, 3): {"name": "Stale disk dirty"},
            (2, 2): {"name": "Disk only", "linked_to": {"bank": 1, "slot": 3}},
        }
        loads = []
        config.list_patches = lambda: [(2, 2), (1, 3), (1, 1)]

        def load_patch(bank, slot):
            loads.append((bank, slot))
            return disk[(bank, slot)]

        config.load_patch = load_patch
        patches = PatchStore()

        # A clean cached patch still wins over a subsequently changed disk
        # representation: get() callers hold that exact live RAM object.
        self.assertEqual("Cached original", patches.get(1, 1)["name"])
        disk[(1, 1)] = {"name": "Newer disk ignored"}
        loads.clear()

        patches.put_patch(1, 3, {
            "name": "Unsaved RAM edit",
            "linked_to": {"bank": 1, "slot": 1},
        }, now_ms=10)
        patches.put_patch(3, 4, {
            "name": "RAM only",
            "linked_to": {"bank": 2, "slot": 2},
        }, now_ms=20)

        got = patches.list()

        self.assertEqual([(1, 1), (1, 3), (2, 2), (3, 4)],
                         [(p["bank"], p["slot"]) for p in got])
        self.assertEqual([
            {"bank": 1, "slot": 1, "name": "Cached original", "dirty": False,
             "linked_to": {"bank": 9, "slot": 1}},
            {"bank": 1, "slot": 3, "name": "Unsaved RAM edit", "dirty": True,
             "linked_to": {"bank": 1, "slot": 1}},
            {"bank": 2, "slot": 2, "name": "Disk only", "dirty": False,
             "linked_to": {"bank": 1, "slot": 3}},
            {"bank": 3, "slot": 4, "name": "RAM only", "dirty": True,
             "linked_to": {"bank": 2, "slot": 2}},
        ], got)
        self.assertEqual([(2, 2)], loads,
                         "cached patches were unnecessarily read from disk")
        self.assertEqual({(1, 1), (1, 3), (3, 4)}, set(patches._cache))

    def test_unreadable_disk_patch_has_empty_metadata_without_caching(self):
        config.list_patches = lambda: [(1, 1), (1, 2)]

        def load_patch(bank, slot):
            if slot == 2:
                raise OSError("simulated transient read failure")
            return {"name": "Readable"}

        config.load_patch = load_patch
        patches = PatchStore()

        self.assertEqual([
            {"bank": 1, "slot": 1, "name": "Readable", "dirty": False},
            {"bank": 1, "slot": 2, "name": "", "dirty": False},
        ], patches.list())
        self.assertEqual({}, patches._cache)

    def test_inventory_enumeration_oserror_preserves_ram_only_entries(self):
        def unavailable():
            raise OSError("simulated filesystem outage")

        config.list_patches = unavailable
        patches = PatchStore()
        patches.put_patch(4, 2, {"name": "Unsaved"}, now_ms=7)

        self.assertEqual([
            {"bank": 4, "slot": 2, "name": "Unsaved", "dirty": True},
        ], patches.list())

    def test_repeated_existence_scans_do_not_fill_cache(self):
        patches = PatchStore()
        patches._cache[(9, 9)] = {"name": "Already cached"}

        with tempfile.TemporaryDirectory(prefix="bosun-store-") as tmp:
            present = Path(tmp) / "present.json"
            present.write_text('{"name":"Present"}', encoding="utf-8")
            missing = Path(tmp) / "missing.json"
            path_calls = []

            def patch_path(bank, slot):
                path_calls.append((bank, slot))
                return str(present if slot == 1 else missing)

            config.patch_path = patch_path
            config.load_patch = lambda *_: (_ for _ in ()).throw(
                AssertionError("has() loaded a complete patch"))

            for _ in range(100):
                self.assertTrue(patches.has(1, 1))
                self.assertFalse(patches.has(1, 2))
                self.assertTrue(patches.has(9, 9))

        self.assertEqual({(9, 9)}, set(patches._cache))
        self.assertEqual(200, len(path_calls))
        self.assertNotIn((9, 9), path_calls,
                         "cached existence should not touch the filesystem")

    def test_clean_cache_is_lru_bounded_and_keeps_current_patch(self):
        loads = []

        def load_patch(bank, slot):
            loads.append((bank, slot))
            return {"name": "Rig %d" % slot, "bindings": []}

        config.load_patch = load_patch
        patches = PatchStore()
        current = patches.get(1, 1)
        patches.protect(1, 1)

        for slot in range(2, 65):
            patches.get(1, slot)
            self.assertLessEqual(len(patches._cache), 2)
            self.assertLessEqual(len(patches._clean_lru), 2)
            self.assertIs(current, patches._cache[(1, 1)])

        self.assertEqual({(1, 1), (1, 64)}, set(patches._cache))
        load_count = len(loads)
        for _ in range(100):
            self.assertIs(current, patches.get(1, 1))
            patches.get(1, 64)
        self.assertEqual(load_count, len(loads),
                         "rapid toggling reloaded the two hot patches")

    def test_clean_cache_uses_recent_access_not_insertion_order(self):
        config.load_patch = lambda bank, slot: {"name": str(slot)}
        patches = PatchStore()

        first = patches.get(1, 1)
        patches.get(1, 2)
        self.assertIs(first, patches.get(1, 1))  # 1 is now most recent
        patches.get(1, 3)

        self.assertEqual({(1, 1), (1, 3)}, set(patches._cache))
        self.assertEqual([(1, 1), (1, 3)], patches._clean_lru)

    def test_transient_reads_do_not_displace_hot_or_dirty_patches(self):
        config.load_patch = lambda bank, slot: {"name": "Disk %d" % slot}
        patches = PatchStore()
        current = patches.get(1, 1)
        patches.protect(1, 1)
        recent = patches.get(1, 2)
        dirty = {"name": "RAM edit"}
        patches.put_patch(1, 3, dirty, now_ms=10)

        for slot in range(4, 100):
            self.assertEqual("Disk %d" % slot,
                             patches.read(1, slot)["name"])

        self.assertIs(current, patches._cache[(1, 1)])
        self.assertIs(recent, patches._cache[(1, 2)])
        self.assertIs(dirty, patches.read(1, 3))
        self.assertEqual({(1, 1), (1, 2), (1, 3)}, set(patches._cache))

    def test_active_put_binding_keeps_current_object_identity(self):
        config.load_patch = lambda bank, slot: {
            "name": "Disk %d" % slot, "bindings": [],
        }
        patches = PatchStore()
        current = patches.get(1, 1)
        patches.protect(1, 1)
        for slot in range(2, 20):
            patches.get(1, slot)

        patches.put_binding(1, 1, {
            "switch": "B", "mode": "latched", "actions": {},
        }, now_ms=50)

        self.assertIs(current, patches.get(1, 1))
        self.assertEqual("B", current["bindings"][0]["switch"])
        self.assertIn((1, 1), patches._dirty_ms)
        for slot in range(20, 40):
            patches.get(1, slot)
        self.assertIs(current, patches._cache[(1, 1)])

    def test_dirty_and_ram_only_patches_are_never_evicted(self):
        config.load_patch = lambda bank, slot: {
            "name": "Disk %d" % slot,
            "bindings": [],
        }
        patches = PatchStore(autosave_enabled=False)
        current = patches.get(1, 1)
        patches.protect(1, 1)
        dirty = {"name": "Dirty", "bindings": []}
        ram_only = {"name": "RAM only", "bindings": []}
        patches.put_patch(1, 2, dirty, now_ms=10)
        patches.put_patch(9, 9, ram_only, now_ms=20)

        for slot in range(3, 40):
            patches.get(1, slot)

        self.assertIs(current, patches._cache[(1, 1)])
        self.assertIs(dirty, patches._cache[(1, 2)])
        self.assertIs(ram_only, patches._cache[(9, 9)])
        self.assertEqual({(1, 2), (9, 9)}, set(patches._dirty_ms))
        self.assertEqual(4, len(patches._cache),
                         "expected two dirty plus two bounded clean patches")

        patches.put_binding(1, 2, {
            "switch": "A", "mode": "tap", "actions": {},
        }, now_ms=30)
        self.assertIs(dirty, patches._cache[(1, 2)])
        self.assertEqual("A", dirty["bindings"][0]["switch"])

    def test_autosave_makes_patch_evictable_only_after_successful_write(self):
        disk = {(1, slot): {"name": "Disk %d" % slot, "bindings": []}
                for slot in range(1, 6)}
        saved = []
        config.load_patch = lambda bank, slot: disk[(bank, slot)]

        def save_patch(bank, slot, patch):
            saved.append((bank, slot, patch["name"]))
            disk[(bank, slot)] = dict(patch)

        config.save_patch = save_patch
        patches = PatchStore(autosave_enabled=True, autosave_debounce_ms=50)
        current = patches.get(1, 1)
        patches.protect(1, 1)
        edited = {"name": "Edited", "bindings": []}
        patches.put_patch(1, 2, edited, now_ms=100)
        patches.get(1, 3)

        patches.tick(149)
        self.assertEqual([], saved)
        self.assertIs(edited, patches._cache[(1, 2)])

        patches.tick(150)
        self.assertEqual([(1, 2, "Edited")], saved)
        self.assertEqual([], patches.dirty_ids())
        self.assertIs(current, patches._cache[(1, 1)])
        self.assertIs(edited, patches._cache[(1, 2)])
        self.assertNotIn((1, 3), patches._cache)

        # It is now safely reloadable from disk, hence eligible for eviction.
        patches.get(1, 4)
        self.assertNotIn((1, 2), patches._cache)
        self.assertEqual("Edited", patches.get(1, 2)["name"])

    def test_failed_read_only_save_retains_unsaved_ram_value(self):
        disk = {"name": "Disk"}
        writable = [False]
        config.load_patch = lambda bank, slot: dict(disk)

        def read_only(_bank, _slot, _patch):
            if not writable[0]:
                raise OSError(30, "read-only filesystem")
            disk.update(_patch)

        config.save_patch = read_only
        patches = PatchStore()
        patches.get(1, 1)
        patches.protect(1, 1)
        unsaved = {"name": "Must survive"}
        patches.put_patch(1, 2, unsaved, now_ms=1)
        patches.save_now(1, 2)
        self.assertFalse(patches.autosave_enabled)
        self.assertEqual([{"bank": 1, "slot": 2}], patches.dirty_ids())

        for slot in range(3, 20):
            patches.get(1, slot)

        self.assertIs(unsaved, patches._cache[(1, 2)])
        self.assertNotIn((1, 2), patches._clean_lru,
                         "failed save was incorrectly declared reloadable")

        writable[0] = True
        self.assertEqual([(1, 2)], patches.save_now(1, 2))
        self.assertEqual([], patches.dirty_ids())
        patches.get(1, 20)
        self.assertNotIn((1, 2), patches._cache)
        self.assertEqual("Must survive", patches.read(1, 2)["name"])

    def test_discarded_patch_rejoins_clean_lru_and_ram_only_failure_drops(self):
        disk = {
            (1, 1): {"name": "Current"},
            (1, 2): {"name": "Disk restored", "linked_to": {"bank": 1, "slot": 1}},
        }

        def load_patch(bank, slot):
            if (bank, slot) not in disk:
                raise OSError("not on disk")
            return dict(disk[(bank, slot)])

        config.load_patch = load_patch
        patches = PatchStore()
        current = patches.get(1, 1)
        patches.protect(1, 1)
        patches.put_patch(1, 2, {"name": "Dirty edit"}, now_ms=1)
        patches.put_patch(1, 3, {"name": "RAM only"}, now_ms=2)

        self.assertEqual([(1, 2), (1, 3)], patches.discard())
        self.assertIs(current, patches._cache[(1, 1)])
        # The restored non-active clean patch may immediately be the LRU
        # victim while the following RAM-only discard is processed. It must
        # remain reloadable with the persisted metadata intact.
        restored = patches.read(1, 2)
        self.assertEqual("Disk restored", restored["name"])
        self.assertEqual({"bank": 1, "slot": 1},
                         restored["linked_to"])
        self.assertNotIn((1, 3), patches._cache)
        self.assertEqual([], patches.dirty_ids())
        self.assertLessEqual(len(patches._clean_lru), 2)

    def test_discard_current_reloads_a_new_pinned_disk_object(self):
        disk = {"name": "Disk current", "bindings": []}
        config.load_patch = lambda _bank, _slot: dict(disk)
        patches = PatchStore()
        old_current = patches.get(1, 1)
        patches.protect(1, 1)
        patches.put_patch(1, 1, {
            "name": "Dirty current", "bindings": [],
        }, now_ms=1)

        self.assertEqual([(1, 1)], patches.discard(1, 1))
        reloaded = patches.get(1, 1)
        patches.protect(1, 1)

        self.assertEqual("Disk current", reloaded["name"])
        self.assertIsNot(old_current, reloaded)
        self.assertIs(reloaded, patches._cache[(1, 1)])
        self.assertEqual((1, 1), patches._protected_key)

    def test_delete_failure_preserves_dirty_ram_value_and_notification_state(self):
        patches = PatchStore()
        unsaved = {"name": "Must survive failed delete", "bindings": []}
        patches.put_patch(7, 3, unsaved, now_ms=91)
        patches.protect(7, 3)
        notifications = []
        patches.on_dirty_changed = lambda: notifications.append(
            list(patches.dirty_ids()))

        failure = OSError(errno.EROFS, "read-only filesystem")
        with mock.patch("os.remove", side_effect=failure):
            with self.assertRaises(OSError) as raised:
                patches.delete(7, 3)

        self.assertIs(failure, raised.exception)
        self.assertIs(unsaved, patches.read(7, 3))
        self.assertEqual([{"bank": 7, "slot": 3}], patches.dirty_ids())
        self.assertEqual((7, 3), patches._protected_key)
        self.assertEqual([], notifications,
                         "failed delete announced a state change")

    def test_delete_failure_preserves_clean_cached_object_and_lru_position(self):
        clean = {"name": "Cached clean", "bindings": []}
        config.load_patch = lambda _bank, _slot: clean
        patches = PatchStore()
        self.assertIs(clean, patches.get(5, 1))
        before_lru = list(patches._clean_lru)

        with mock.patch("os.remove",
                        side_effect=OSError(errno.EIO, "I/O failure")):
            with self.assertRaises(OSError):
                patches.delete(5, 1)

        self.assertIs(clean, patches.get(5, 1))
        self.assertEqual(before_lru, patches._clean_lru)
        self.assertEqual([], patches.dirty_ids())

    def test_delete_enoent_is_idempotent_and_discards_ram_only_patch(self):
        patches = PatchStore()
        ram_only = {"name": "Never persisted", "bindings": []}
        patches.put_patch(8, 4, ram_only, now_ms=11)
        notifications = []
        patches.on_dirty_changed = lambda: notifications.append(
            list(patches.dirty_ids()))

        with mock.patch("os.remove",
                        side_effect=OSError(errno.ENOENT, "not found")):
            patches.delete(8, 4)
            # Deleting an already absent location is also a successful no-op.
            patches.delete(8, 4)

        self.assertNotIn((8, 4), patches._cache)
        self.assertEqual([], patches.dirty_ids())
        self.assertEqual([[]], notifications,
                         "an already-absent patch changed no dirty state")
        self.assertIsNone(patches._protected_key)

    def test_successful_delete_unlinks_before_committing_dirty_ram_state(self):
        with tempfile.TemporaryDirectory(prefix="bosun-store-delete-") as tmp:
            path = Path(tmp) / "patch.json"
            path.write_text('{"name":"Persisted"}', encoding="utf-8")
            config.patch_path = lambda _bank, _slot: str(path)
            config.load_patch = lambda _bank, _slot: {"name": "Persisted"}
            patches = PatchStore()
            patches.get(4, 6)
            dirty = {"name": "Unsaved replacement"}
            patches.put_patch(4, 6, dirty, now_ms=17)
            patches.protect(4, 6)
            notifications = []
            patches.on_dirty_changed = lambda: notifications.append(
                list(patches.dirty_ids()))

            patches.delete(4, 6)

            self.assertFalse(path.exists())
            self.assertNotIn((4, 6), patches._cache)
            self.assertNotIn((4, 6), patches._clean_lru)
            self.assertEqual([], patches.dirty_ids())
            self.assertIsNone(patches._protected_key)
            self.assertEqual([[]], notifications)


if __name__ == "__main__":
    unittest.main(verbosity=2)
