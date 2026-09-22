"""Build native plugins from shared schemas and small model overrides.

The same resolved model definitions generate the editor manifest and C runtime
capabilities. Existing Player message IDs and device.kemper settings stay valid.
"""
import argparse
from copy import deepcopy
import json
import re
import sys
from pathlib import Path


def manifest_values(root):
    sys.path.insert(0, str(root / "tools"))
    from build_manifest_tail import load_manifest_values
    core, plugins = load_manifest_values(root)
    # Native-only extensions; CircuitPython remains frozen.
    core["captain_patch"]["params"]["bank"]["max"] = 125
    looper = plugins["kemper_player"]["messages"]["kemper_looper"]
    looper["params"]["action"]["values"] = list(looper["params"]["action"]["values"]) + ["cancel_overdub", "erase"]
    looper["params"]["state"] = {
        "type": "enum", "values": ["tap", "press", "release"], "default": "tap", "label": "Button action"
    }
    looper["summary"] = "Looper {action} {state}"
    runtime = (root / "firmware-native/src/runtime.c").read_text(encoding="utf-8")
    supported = set(re.findall(r'"([a-z_]+)"', runtime.split("supported[] = {", 1)[1].split("};", 1)[0]))
    selected = {"generic_midi": plugins["generic_midi"]}
    definitions = json.loads((root / "firmware-native/plugins/kemper.json").read_text(encoding="utf-8"))
    models = {}
    for kind, overrides in definitions.items():
        parent = overrides.get("extends")
        model = dict(models[parent]) if parent else {
            "max_banks": plugins[kind]["messages"]["kemper_rig"]["params"]["bank"]["max"],
            "unsupported_messages": [],
            "profiler": False,
        }
        model.update(overrides)
        models[kind] = model
        plugin = deepcopy(selected[parent] if parent else plugins[kind])
        plugin["label"] = model.get("label", plugin["label"])
        plugin["config_schema"]["label"] = plugin["label"] + " target"
        plugin["config_schema"]["fields"].update(deepcopy(model.get("config_fields", {})))
        if "hint" in model:
            plugin["config_schema"]["hint"] = model["hint"]
        for message in model["unsupported_messages"]:
            plugin["messages"].pop(message, None)
        if parent:
            rig = plugin["messages"]["kemper_rig"]
            rig["label"] = "Select Performance Slot"
            rig["params"]["bank"].update(max=model["max_banks"], label=f"Performance (1-{model['max_banks']})")
            rig["params"]["rig"]["label"] = "Slot (1-5)"
            rig["summary"] = "Performance {bank}, slot {rig}"
            rig["requires"] = {"mode": "performance"}
            plugin["messages"]["kemper_step_rig"]["label"] = "Step Rig / Performance"
            plugin["messages"]["kemper_fixed_toggle"]["requires"] = {"generation": "MK2"}
            plugin["messages"]["kemper_browse_rig"] = {
                "label": "Select Browse MIDI Program",
                "params": {
                    "program": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Assigned MIDI program (0-127)"},
                    "channel": deepcopy(rig["params"]["channel"]),
                },
                "summary": "Browse program {program}",
                "requires": {"mode": "browse"},
            }
        selected[kind] = plugin
    declared = set(core)
    for plugin in selected.values():
        declared.update(plugin["messages"])
    if declared != supported:
        raise ValueError(f"Native manifest/runtime mismatch: {declared ^ supported}")
    return core, selected, models


def generate(root):
    core, selected, _ = manifest_values(root)
    fields = json.dumps({"core_messages": core, "plugins": selected}, ensure_ascii=True,
                        sort_keys=True, separators=(",", ":"))[1:-1]
    value = "," + fields
    # Adjacent C literals preserve escape sequences without long source lines.
    chunks = [json.dumps(value[i:i + 120]) for i in range(0, len(value), 120)]
    return "/* Generated from shared schemas and native plugins; do not edit. */\n" + \
        "static const char BOSUN_MANIFEST_FIELDS[] =\n" + "\n".join(chunks) + ";\n"


def generate_models(root):
    _, _, models = manifest_values(root)
    lines = ["/* Generated Kemper model differences; do not edit. */"]
    for kind, model in models.items():
        excluded = ",".join(json.dumps(name) for name in model["unsupported_messages"])
        lines.append(f"static const char *const {kind}_excluded[] = {{{excluded + ',' if excluded else ''}NULL}};")
    lines.append("static const bosun_kemper_model kemper_models[] = {")
    for kind, model in models.items():
        lines.append(f'    {{"{kind}", {model["product_id"]}, {model["max_banks"]}, '
                     f'{str(model["fixed_effects"]).lower()}, {str(model["profiler"]).lower()}, {kind}_excluded}},')
    return "\n".join(lines + ["};", ""])


def generate_kinds(root):
    _, plugins, _ = manifest_values(root)
    return "/* Generated from the native manifest; do not edit. */\n" + \
        "static const char *const bosun_plugin_kinds[] = {" + \
        ",".join(json.dumps(kind) for kind in plugins) + "};\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models-output", type=Path, required=True)
    parser.add_argument("--kinds-output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    for path, generator in ((args.output, generate), (args.models_output, generate_models),
                            (args.kinds_output, generate_kinds)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generator(root), encoding="ascii")
