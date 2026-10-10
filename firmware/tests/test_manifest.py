"""Native schemas: PROFILER models share the Player schema except their own overrides."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("native_manifest", ROOT / "firmware/cmake/build_manifest.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ManifestModels(unittest.TestCase):
    def test_native_ranges_and_looper_actions(self):
        core, plugins, _ = build.manifest_values(ROOT)
        self.assertEqual(core["captain_patch"]["params"]["bank"]["max"], 125)
        looper = plugins["kemper_player"]["messages"]["kemper_looper"]["params"]
        self.assertIn("cancel_overdub", looper["action"]["values"])
        self.assertIn("erase", looper["action"]["values"])
        self.assertEqual(looper["state"]["default"], "tap")

    def test_profiler_shares_the_player_schema(self):
        _, plugins, models = build.manifest_values(ROOT)
        player, head = plugins["kemper_player"], plugins["kemper_head"]
        self.assertEqual(player["config_schema"]["key"], head["config_schema"]["key"])
        for name, schema in player["config_schema"]["fields"].items():
            self.assertEqual(schema, head["config_schema"]["fields"][name])
        self.assertEqual(head["config_schema"]["fields"]["generation"]["default"], "MK1")
        self.assertEqual(head["config_schema"]["fields"]["mode"]["default"], "performance")
        self.assertEqual(player["default_layout"], head["default_layout"])
        self.assertEqual(player["tft_fields"], head["tft_fields"])
        self.assertEqual(head["messages"]["kemper_rig"]["params"]["bank"]["max"], 125)
        for name, schema in head["messages"].items():
            if name not in ("kemper_rig", "kemper_step_rig", "kemper_fixed_toggle", "kemper_browse_rig"):
                self.assertEqual(schema, player["messages"][name])
        self.assertEqual(set(player["messages"]) - set(head["messages"]),
                         set(models["kemper_head"]["unsupported_messages"]))
        head["messages"]["kemper_morph"]["params"]["value"]["max"] = 10
        self.assertEqual(player["messages"]["kemper_morph"]["params"]["value"]["max"], 127)

    def test_checked_in_plugin_kinds_follow_the_manifest(self):
        # manifest_values() already rejects a stale include/bosun/plugin_kinds.h.
        _, plugins, _ = build.manifest_values(ROOT)
        self.assertEqual(list(plugins), ["generic_midi", "kemper_player", "kemper_head"])
        stale = 'static const char *const bosun_plugin_kinds[] = {"generic_midi", "kemper_player"};'
        with self.assertRaisesRegex(ValueError, "plugin_kinds.h"):
            build.check_plugin_kinds(stale, plugins)


if __name__ == "__main__":
    unittest.main()
