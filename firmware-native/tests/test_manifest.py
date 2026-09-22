"""Model overrides share schemas; native corrections preserve existing IDs."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("native_manifest", ROOT / "firmware-native/cmake/build_manifest.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ManifestModels(unittest.TestCase):
    def test_shared_schemas_and_player_compatibility(self):
        _, plugins, models = build.manifest_values(ROOT)
        from build_manifest_tail import load_manifest_values
        _, legacy = load_manifest_values(ROOT)
        player, head = plugins["kemper_player"], plugins["kemper_head"]
        self.assertEqual(set(player["messages"]), set(legacy["kemper_player"]["messages"]))
        for name, schema in player["messages"].items():
            if name != "kemper_looper":
                self.assertEqual(schema, legacy["kemper_player"]["messages"][name])
        self.assertEqual(player["messages"]["kemper_looper"]["params"]["state"]["default"], "tap")
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


if __name__ == "__main__":
    unittest.main()
