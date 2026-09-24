"""Generate the final Bosun stand with a horizontal front-to-rear display arc."""
from pathlib import Path
import itertools
import json
import math

import cadquery as cq
import numpy as np
import trimesh

import cad_geometry as g

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "final_stand"


def sample_path(points, step=2):
    samples = []
    for (a0, d0), (a1, d1) in zip(points, points[1:]):
        n = max(1, math.ceil(max(abs(a1-a0), abs(d1-d0))/step))
        samples.extend((a0+(a1-a0)*t, d0+(d1-d0)*t)
                       for t in np.linspace(0, 1, n+1))
    return samples


def validate_motion(parts, refs, p, paths, poses):
    """Check the complete motion with face gears disengaged."""
    states = [(a, d, 1.4) for name, path in paths.items()
              for a, d in sample_path(path, .5 if name == "front_to_rear_flat" else 2)]
    states += [(a, d, 0) for a, d in poses.values()]
    pitch = 360.0 / p["joint_teeth"]
    horizontal_stops = [[i*pitch, 90] for i in range(-7, 6)]
    states += [(a, d, 0) for a, d in horizontal_stops]
    obstacles = [v for v in refs if v["group"] == "reference_fixed"]
    cable = g.box(-p["captain_width"]/2, p["captain_depth"]/2, 4,
                  p["captain_width"], p["cable_corridor_depth"], p["cable_corridor_top"]-4)
    minimum_moving = float("inf")
    for index, (arm, display_angle, release) in enumerate(states):
        if index % 25 == 0:
            print(f"  Motion: {index}/{len(states)} poses checked", flush=True)
        printed = [(v["name"], g.fold_pose(v, arm, display_angle, release, p)) for v in parts]
        display = [(v["name"], g.pose(v, arm, display_angle, p)) for v in refs
                   if v["group"] == "reference_moving"]
        for name, shape in printed + display:
            assert not g.overlaps(shape, cable), ("cables", name, arm, display_angle)
            for obs in obstacles:
                assert not g.overlaps(shape, obs["shape"]), ("Captain", name, obs["name"], arm, display_angle)
            zmin = shape.BoundingBox().zmin
            assert zmin > -.001, ("floor", name, arm, display_angle, zmin)
            if not name.startswith(("01_", "02_")):
                minimum_moving = min(minimum_moving, zmin)
        for (n1, s1), (n2, s2) in itertools.combinations(printed, 2):
            assert not g.overlaps(s1, s2), (n1, n2, arm, display_angle)
        for (n1, s1), (n2, s2) in itertools.product(printed, display):
            assert not g.overlaps(s1, s2), (n1, n2, arm, display_angle)
    return {"path_samples": len(states), "step_max_deg": 2, "paths": paths,
            "minimum_moving_floor_clearance_mm": minimum_moving,
            "static_poses": poses, "horizontal_stops": horizontal_stops,
            "horizontal_sample_step_max_deg": .5, "release_mm": 1.4,
            "note": "Face gears are separated by 1.4 mm during movement."}


def main():
    p = json.loads((ROOT / "parameters.json").read_text(encoding="utf-8"))
    p["pivot_y"], p["pivot_z"] = g.pivot_position(p, p["arm_angle_deg"])
    for folder in ("step", "stl", "images"):
        (OUT / folder).mkdir(parents=True, exist_ok=True)
    parts, refs = g.make_model(p)
    poses = {"rear": [p["arm_angle_deg"], p["tilt_deg"]],
             "front": [p["front_arm_deg"], p["front_display_deg"]],
             "rear_flat": [p["rear_flat_arm_deg"], 90],
             "transport": [p["folded_arm_deg"], p["folded_display_deg"]]}
    paths = {"rear_to_transport": g.fold_waypoints(p),
             "transport_to_front": [poses["transport"],
                 [p["front_transition_arm_deg"], p["folded_display_deg"]],
                 [p["front_transition_arm_deg"], p["front_display_deg"]], poses["front"]],
             "front_to_rear_flat": [poses["front"], poses["rear_flat"]],
             "rear_flat_to_rear": [poses["rear_flat"],
                 [p["horizontal_rear_transition_arm_deg"], 90],
                 [p["horizontal_rear_transition_arm_deg"], p["rear_transition_display_deg"]],
                 [p["arm_angle_deg"], p["rear_transition_display_deg"]], poses["rear"]]}
    tooth_step = 360.0 / p["joint_teeth"]
    for mode, (arm, display_angle) in poses.items():
        assert abs(arm/tooth_step-round(arm/tooth_step)) < 1e-7, (mode, "arm index")
        assert abs((display_angle-arm)/tooth_step-round((display_angle-arm)/tooth_step)) < 1e-7, (mode, "display index")
    print("Validating solids and the rear position...", flush=True)
    report = g.validate(parts, refs, p)
    report["display_reference"] = p["display_reference"]
    body = next(v["shape"] for v in refs if v["name"] == "Captain_nominal_envelope")
    display_ref = next(v for v in refs if v["name"] == "Display_nominal_envelope")
    front_shape = g.pose(display_ref, *poses["front"], p)
    distance = body.distance(front_shape)
    front_gap = {"minimum_distance_mm": distance,
        "front_clearance_y_mm": body.BoundingBox().ymin-front_shape.BoundingBox().ymax}
    assert distance > .001, ("display contact", distance)
    assert 0 < front_gap["front_clearance_y_mm"] <= p["front_gap_max_mm"], front_gap
    assert front_shape.BoundingBox().zlen < p["display_thickness"]+.001, "Display is not horizontal"
    horizontal = {"display_deg": 90, "arm_min_deg": p["front_arm_deg"],
                  "arm_max_deg": p["rear_flat_arm_deg"], "step_deg": tooth_step,
                  "waypoints": paths["front_to_rear_flat"],
                  "instruction": "Hold the display level by hand with both pivot joints disengaged."}
    report["front_gap"] = front_gap
    report["display_support"]["note"] = "Rear support band within 25 mm; circular mounting ears are 30 mm in diameter and 8 mm thick."
    # The preview can be checked in parallel; production reports and exports
    # are completed only after all geometry checks have passed.
    scene = {"parameters": p, "validation_pending": True,
             "parts": [{"name": v["name"], "group": v["group"], "color": v["color"],
                        **g.mesh_data(v["shape"])} for v in parts+refs],
             "poses": poses, "motion_paths": paths, "front_gap": front_gap,
             "horizontal_motion": horizontal,
             "folding": {"waypoints": paths["rear_to_transport"], "release_mm": 1.4},
             "clearance_envelope": report["clearance_envelope"]}
    (OUT / "scene.json").write_text(json.dumps(scene, separators=(",", ":")), encoding="utf-8")
    print("Checking front, rear and transport motion...", flush=True)
    report["motion"] = validate_motion(parts, refs, p, paths, poses)
    report["captain_profile"] = g.captain_profile(p)
    # Individual STEP parts use their local modeling coordinates.
    for part in parts:
        name = part["name"]
        cq.exporters.export(part["shape"], str(OUT / "step" / f"{name}.step"))
        stl = OUT / "stl" / f"{name}.stl"
        cq.exporters.export(g.print_shape(part), str(stl), tolerance=.08, angularTolerance=.12)
        mesh = trimesh.load_mesh(stl)
        assert mesh.is_watertight and mesh.is_volume and len(mesh.split()) == 1, name
        next(row for row in report["parts"] if row["part"] == name)["stl_watertight"] = True
    envelopes = {}
    for mode, (arm, display_angle) in poses.items():
        assembly = cq.Assembly(name="Bosun_final_" + mode)
        solids = []
        for part in parts + refs:
            shape = g.pose(part, arm, display_angle, p)
            color = cq.Color(*[int(part["color"][i:i+2], 16)/255 for i in (1, 3, 5)])
            assembly.add(shape, name=part["name"], color=color)
            solids.append(shape)
        assembly.save(str(OUT / f"stand_{mode}_with_references.step"))
        b = cq.Compound.makeCompound(solids).BoundingBox()
        envelopes[mode] = [b.xlen, b.ylen, b.zlen]
    report["envelopes_mm"] = envelopes
    front_arm, front_display = poses["front"]
    screen = next(v for v in refs if v["name"] == "Display_nominal_envelope")
    low_switch = next(v for v in refs if v["name"] == "Switch_reference_-116_-25")
    s = g.pose(screen, front_arm, front_display, p)
    sw = low_switch["shape"]
    ergonomic = {"display_top_mm": s.BoundingBox().zmax,
                 "low_switch_top_mm": sw.BoundingBox().zmax,
                 "height_difference_mm": sw.BoundingBox().zmax-s.BoundingBox().zmax}
    report["front_switch_height_comparison"] = ergonomic
    report["limitations"] += ["Footswitch access with the display in front requires testing with actual footwear."]
    scene.update(validation_pending=False, front_switch_height_comparison=ergonomic)
    (OUT / "geometry_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "scene.json").write_text(json.dumps(scene, separators=(",", ":")), encoding="utf-8")
    print(f"Exported {len(parts)} closed STL meshes, STEP parts and four assemblies.", flush=True)


if __name__ == "__main__":
    main()
