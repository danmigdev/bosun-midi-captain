"""Package Bosun Stand sources, printable parts and verified deliverables."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "final_stand"


def write_print_archive(path, names, reference_note):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(OUT / "stl" / f"{name}.stl", f"{name}.stl")
        archive.write(reference_note, reference_note.name)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None and len(archive.namelist()) == len(names) + 1
        assert archive.read(reference_note.name) == reference_note.read_bytes()
        for name in names:
            assert archive.read(f"{name}.stl") == (OUT / "stl" / f"{name}.stl").read_bytes()


def main():
    geometry = json.loads((OUT / "geometry_report.json").read_text(encoding="utf-8"))
    independent = json.loads((OUT / "independent_report.json").read_text(encoding="utf-8"))
    browser = json.loads((OUT / "browser_report.json").read_text(encoding="utf-8"))
    scene = json.loads((OUT / "scene.json").read_text(encoding="utf-8"))
    assert scene["validation_pending"] is False
    assert 0 < scene["front_gap"]["front_clearance_y_mm"] <= scene["parameters"]["front_gap_max_mm"]
    assert scene["horizontal_motion"]["display_deg"] == 90
    assert independent["status"].startswith("PASS")
    assert independent["horizontal_arc"]["collisions"] == 0
    assert len(independent["horizontal_arc"]["clamped_stops"]) == 13
    assert independent["horizontal_arc"]["display_surface_horizontal_and_facing_up"]
    assert len(independent["radial_teeth"]) == 8
    assert all(row["count"] == 28 for row in independent["radial_teeth"])
    assert browser["language"] == "en"
    assert not browser["runtime_errors"]
    assert browser["independent_arm_and_display_controls"]
    assert browser["free_angles_retained_on_release"]
    assert browser["explicit_snap_to_teeth"] and browser["free_angle_clearance"]
    assert browser["camera_full_rotation"]
    assert len(geometry["parts"]) == 13 and all(row["stl_watertight"] for row in geometry["parts"])
    part_names = sorted(row["part"] for row in geometry["parts"])
    assert {path.stem for path in (OUT / "stl").glob("*.stl")} == set(part_names)
    reference_note = OUT / "DISPLAY_REFERENCE.md"
    print_archive = ROOT / "Bosun_Stand_Print_Files.zip"
    write_print_archive(print_archive, part_names, reference_note)
    arms_archive = ROOT / "Bosun_Stand_Final_2_Arms.zip"
    write_print_archive(arms_archive, ("03_arm_right", "03_arm_left"), reference_note)
    sources = OUT / "sources"
    sources.mkdir(exist_ok=True)
    for name in ("cad_geometry.py", "generate_stand.py",
                 "parameters.json", "verify_stand.py",
                 "build_preview.py", "preview_template.html", "renderer_webgl.js", "motion_clearance.js",
                 "package_stand.py", "requirements.txt"):
        shutil.copy2(ROOT / name, sources / name)
    files = sorted(f for f in OUT.rglob("*") if f.is_file() and f.name != "manifest.json")
    manifest = {"version": scene["parameters"]["version"], "status": "final", "language": "en", "files": {
        f.relative_to(OUT).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    complete = ROOT / "Bosun_Stand_Final_Complete.zip"
    with zipfile.ZipFile(complete, "w", zipfile.ZIP_DEFLATED) as z:
        for file in files + [OUT / "manifest.json"]:
            z.write(file, file.relative_to(OUT).as_posix())
    with zipfile.ZipFile(complete) as z:
        assert z.testzip() is None
        for name, sha in manifest["files"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == sha
    print(f"Created {print_archive.name} (13 STL files), {arms_archive.name}, and {complete.name}.", flush=True)


if __name__ == "__main__":
    main()
