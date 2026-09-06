"""Generate only schemas implemented by the native runtime, from CP sources."""
import argparse
import json
import re
import sys
from pathlib import Path


def generate(root):
    sys.path.insert(0, str(root / "tools"))
    from build_manifest_tail import load_manifest_values
    core, plugins = load_manifest_values(root)
    runtime = (root / "firmware-native/src/runtime.c").read_text(encoding="utf-8")
    supported = set(re.findall(r'"([a-z_]+)"', runtime.split("supported[] = {", 1)[1].split("};", 1)[0]))
    selected = {name: plugins[name] for name in ("generic_midi", "kemper_player")}
    declared = set(core)
    for plugin in selected.values():
        declared.update(plugin["messages"])
    if declared != supported:
        raise ValueError(f"Native manifest/runtime mismatch: {declared ^ supported}")
    fields = json.dumps({"core_messages": core, "plugins": selected}, ensure_ascii=True,
                        sort_keys=True, separators=(",", ":"))[1:-1]
    value = "," + fields
    # Adjacent C literals preserve escape sequences without long source lines.
    chunks = [json.dumps(value[i:i + 120]) for i in range(0, len(value), 120)]
    return "/* Generated from canonical CP schemas; do not edit. */\n" + \
        "static const char BOSUN_MANIFEST_FIELDS[] =\n" + "\n".join(chunks) + ";\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generate(Path(__file__).resolve().parents[2]), encoding="ascii")
