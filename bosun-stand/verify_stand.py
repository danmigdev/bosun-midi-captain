"""Independently verify the final stand by reopening exported STEP and STL files.

The stand generator is not imported: dimensions, sections and part comparisons
are derived from the STEP solids. Front clearance is recalculated from
independent references. Results are written to final_stand/independent_report.json.
"""

from pathlib import Path
import argparse
import json
import math
import traceback

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface
import trimesh


ROOT = Path(__file__).resolve().parent
NAMES = ([f"{prefix}_{side}" for prefix in ("01_base", "02_clamp_plate", "03_arm")
          for side in ("right", "left")]
         + ["04_display_crossbar", "06_spacer_1", "06_spacer_2"]
         + [f"07_{level}_knob_{side}" for level in ("lower", "upper") for side in ("right", "left")]
         )


def box(x, y, z, dx, dy, dz):
    return cq.Workplane("XY").box(dx, dy, dz, centered=False).translate((x, y, z)).val()


def cylinder(radius, length, point, axis):
    return cq.Solid.makeCylinder(radius, length, cq.Vector(*point), cq.Vector(*axis))


def read_step(path):
    result = cq.importers.importStep(str(path)).val()
    assert result.isValid(), f"Invalid STEP: {path.name}"
    assert len(result.Solids()) == 1, f"STEP must contain exactly one solid: {path.name}"
    return result


def require_close(actual, expected, tolerance=0.002, label="dimension"):
    assert abs(actual - expected) <= tolerance, f"{label}: {actual} != {expected}"
    return round(float(actual), 6)


def symmetric_difference(a, b):
    return a.cut(b).Volume() + b.cut(a).Volume()


def require_equal_shape(a, b, label):
    difference = symmetric_difference(a, b)
    assert difference < 0.01, f"Shape mismatch: {label}: {difference} mm3"
    return round(difference, 9)


def cylindrical_faces(shape, radius, axis):
    result = []
    for face in shape.Faces():
        if face.geomType() != "CYLINDER":
            continue
        surface = BRepAdaptor_Surface(face.wrapped).Cylinder()
        direction = surface.Axis().Direction()
        vector = (direction.X(), direction.Y(), direction.Z())
        if abs(surface.Radius() - radius) < 0.001 and abs(vector[axis]) > 0.99999:
            point = surface.Location()
            result.append((face, (point.X(), point.Y(), point.Z())))
    return result


def pivot_yz(shape):
    centers = {(round(point[1], 6), round(point[2], 6))
               for _, point in cylindrical_faces(shape, 3.3, 0)}
    assert len(centers) == 1, f"Unique crossbar axis not found: {centers}"
    return next(iter(centers))


def tooth_count(shape, x, center_y, center_z, expected, label):
    """Count actual radial crest/valley vertices at the inner radius.

    Each tooth has a vertex at radius 9 mm at the extreme axial coordinate.
    Coordinates come from reimported STEP solids, not from CAD parameters.
    """
    angles = set()
    for vertex in shape.Vertices():
        point = vertex.Center()
        radius = math.hypot(point.y - center_y, point.z - center_z)
        if abs(point.x - x) < .0001 and abs(radius - 9) < .0001:
            angle = math.degrees(math.atan2(point.z - center_z, point.y - center_y)) % 360
            angles.add(round(angle % 360, 5) % 360)
    angles = sorted(angles)
    assert len(angles) == expected, f"Tooth count {label}: {len(angles)} != {expected}"
    step = 360 / expected
    spacing = [(angles[(i + 1) % len(angles)] - angles[i]) % 360 for i in range(len(angles))]
    assert max(abs(value - step) for value in spacing) < .0001, f"Nonuniform teeth: {label}"
    return {"surface": label, "count": len(angles), "uniform_pitch_deg": step,
            "measured_pitch_min_deg": min(spacing), "measured_pitch_max_deg": max(spacing),
            "measurement_radius_mm": 9, "measured_axial_level_mm": x}


def overlaps(a, b):
    aa, bb = a.BoundingBox(), b.BoundingBox()
    if any(getattr(aa, k + "max") <= getattr(bb, k + "min") + .001 or
           getattr(bb, k + "max") <= getattr(aa, k + "min") + .001 for k in "xyz"):
        return False
    return a.intersect(b).Volume() > .01


def pose_core(name, shape, arm_angle, display_angle, p, initial_pivot):
    by, bz = p["base_pivot_y"], p["base_pivot_z"]
    if name.startswith(("03_", "07_")):
        return shape.rotate((0, by, bz), (1, by, bz), -arm_angle)
    if name.startswith(("04_", "06_")):
        py, pz = initial_pivot
        target_y = by + p["arm_length"] * math.sin(math.radians(arm_angle))
        target_z = bz + p["arm_length"] * math.cos(math.radians(arm_angle))
        return shape.rotate((0, py, pz), (1, py, pz), -display_angle).translate(
            (0, target_y - py, target_z - pz))
    return shape


def verify(p, out):
    report = {"status": "RUNNING", "method": "Independent verification of all 13 STEP/STL parts using dimensions, sections, teeth, nut pockets, screw passages and collision checks; no stand generator is imported. Captain and display envelopes are independently reconstructed from dimensions; clearance is measured between solids."}
    actual_step_names = {path.stem for path in (out / "step").glob("*.step")}
    actual_stl_names = {path.stem for path in (out / "stl").glob("*.stl")}
    assert actual_step_names == set(NAMES), f"Missing/extra STEP files: {actual_step_names ^ set(NAMES)}"
    assert actual_stl_names == set(NAMES), f"Missing/extra STL files: {actual_stl_names ^ set(NAMES)}"
    shapes = {name: read_step(out / "step" / (name + ".step")) for name in NAMES}
    report["reimport"] = []
    for name, shape in shapes.items():
        mesh = trimesh.load_mesh(out / "stl" / (name + ".stl"))
        assert mesh.is_watertight and mesh.is_volume, f"STL is open or has invalid volume: {name}"
        assert len(mesh.split()) == 1, f"Disconnected STL: {name}"
        relative_error = abs(mesh.volume - shape.Volume()) / shape.Volume()
        assert relative_error < 0.005, f"STL/STEP volume mismatch: {name}: {relative_error}"
        report["reimport"].append({"part": name, "valid_step": True, "solids": 1,
            "stl_watertight": True, "stl_components": 1,
            "step_volume_cm3": round(shape.Volume() / 1000, 4),
            "stl_volume_relative_error": round(relative_error, 7)})

    py, pz = pivot_yz(shapes["04_display_crossbar"])

    root_x = max(p["captain_width"] / 2 + 14,
                 p["display_width"] / 2 + p["display_side_clearance"] + 8)
    ear_x = root_x - 8
    report["arms"] = []
    for side in ("right", "left"):
        shape = shapes["03_arm_" + side]
        if side == "left":
            shape = shape.mirror("YZ")
        holes = sorted({(round(point[1], 6), round(point[2], 6))
                        for _, point in cylindrical_faces(shape, 3.3, 0)})
        assert len(holes) == 2, f"Expected two arm axes: {holes}"
        holes.sort(key=lambda point: point[1])
        length = require_close(holes[1][1] - holes[0][1], 106, label="arm center distance")
        require_close(holes[0][0], holes[1][0], label="arm hole alignment")
        by, bz = holes[0]
        web = shape.intersect(box(root_x - 2, by - 1, bz + length / 2 - .1, 12, 2, .2))
        web_thickness = require_close(web.BoundingBox().xlen, 4, label="arm web thickness")
        discs = []
        for center_y, center_z in holes:
            strip = shape.intersect(box(root_x - 2, center_y + 14.8, center_z - .01, 12, .1, .02))
            thickness = require_close(strip.BoundingBox().xlen, 5, label="disc thickness")
            outer = [(face, point) for face, point in cylindrical_faces(shape, 15, 0)
                     if abs(point[1] - center_y) < .001 and abs(point[2] - center_z) < .001]
            assert outer, "R15 cylindrical disc face not found"
            discs.append({"center_yz_mm": [center_y, center_z], "radius_mm": 15, "thickness_mm": thickness})
        report["arms"].append({"side": side, "center_distance_mm": length,
                                "web_thickness_mm": web_thickness, "discs": discs})

    bracket = shapes["04_display_crossbar"]
    report["round_ears"] = []
    for side in ("right", "left"):
        current = bracket if side == "right" else bracket.mirror("YZ")
        section_x, thickness = ear_x + 7, .05
        section = current.intersect(box(section_x, py - 20, pz - 20, thickness, 40, 40))
        ideal = cylinder(15, thickness, (section_x, py, pz), (1, 0, 0)).cut(
            cylinder(3.3, thickness, (section_x, py, pz), (1, 0, 0)))
        section_error = require_equal_shape(section, ideal, "round mounting ear " + side)
        require_close(section.BoundingBox().ylen, 30, label="mounting ear diameter Y")
        require_close(section.BoundingBox().zlen, 30, label="mounting ear diameter Z")
        body_clip = cylinder(15.01, 8, (ear_x, py, pz), (1, 0, 0))
        body_section = current.intersect(body_clip)
        require_close(body_section.BoundingBox().xlen, 8, label="mounting ear thickness")
        report["round_ears"].append({"side": side, "outer_diameter_mm": 30,
            "arm_disc_outer_diameter_mm": 30, "body_thickness_mm": 8,
            "section_offset_from_inner_face_mm": 7,
            "ring_section_symmetric_difference_mm3": section_error})

    expected_teeth = int(p["joint_teeth"])
    assert expected_teeth == 28, "The final design requires 28 evenly spaced teeth"
    report["radial_teeth"] = []
    for side in ("right", "left"):
        canonical = lambda shape: shape if side == "right" else shape.mirror("YZ")
        shoe = canonical(shapes["01_base_" + side])
        arm = canonical(shapes["03_arm_" + side])
        ear = canonical(bracket)
        by, bz = p["base_pivot_y"], p["base_pivot_z"]
        male_x = root_x + p["joint_tooth_height"]
        female_x = root_x + p["joint_axial_clearance"]
        report["radial_teeth"].append(tooth_count(shoe, male_x, by, bz, expected_teeth, "base_" + side))
        report["radial_teeth"].append(tooth_count(arm, female_x, by, bz, expected_teeth, "arm_lower_" + side))
        report["radial_teeth"].append(tooth_count(arm, female_x, by, bz + p["arm_length"], expected_teeth, "arm_upper_" + side))
        report["radial_teeth"].append(tooth_count(ear, male_x, py, pz, expected_teeth, "crossbar_" + side))

    display_top_relative = p["display_height"] / 2 - p["display_pivot_offset_z"]
    band_section = bracket.intersect(box(-.1, py + 8, pz + display_top_relative - 10, .2, 15, .1))
    band_thickness = require_close(band_section.BoundingBox().ylen, 5, label="rear band thickness")
    mount_holes = sorted({(round(point[0], 6), round(point[2], 6))
                         for _, point in cylindrical_faces(bracket, 2.25, 1)})
    assert len(mount_holes) == 2, f"Display mounting holes: {mount_holes}"
    pitch = require_close(mount_holes[1][0] - mount_holes[0][0], 75, label="display mounting hole pitch")
    require_close(mount_holes[0][1], mount_holes[1][1], label="display mounting hole height")
    boss_section = bracket.intersect(box(mount_holes[1][0] + 5, py + 8,
        mount_holes[1][1] - .05, .1, 15, .1))
    boss_thickness = require_close(boss_section.BoundingBox().ylen, 7, label="mounting hole boss thickness")
    report["display_bracket"] = {"band_thickness_mm": band_thickness,
        "boss_thickness_mm": boss_thickness, "mount_hole_pitch_mm": pitch,
        "mount_hole_diameter_mm": 4.5, "pivot_yz_mm": [py, pz]}

    # Measure the top wall plane above the shelf plane.
    # The hub and disc are separate raised features: extrapolate the continuous
    # sloping face to Y +/-36, including the area covered by the hub.
    base_top = p["base_shelf_thickness"] + p["base_foot_height"]
    expected_front = float(p["base_wall_front_height"])
    expected_rear = float(p["base_wall_rear_height"])
    expected_slope = (expected_rear - expected_front) / 72
    wall_x = p["captain_width"] / 2 + 4
    wall_thickness = root_x - wall_x
    support_z = base_top + 1
    rise = p["captain_rear_height_ground"] - p["captain_front_height_ground"]
    run = math.sqrt(p["captain_top_surface_depth"] ** 2 - rise ** 2)
    slope_angle = math.degrees(math.atan2(rise, run))
    rear_y = p["captain_depth"] / 2
    front_y = rear_y - run
    front_body = p["captain_front_height_ground"] - p["captain_original_foot_height"]
    rear_body = p["captain_rear_height_ground"] - p["captain_original_foot_height"]
    cap_z = support_z + rear_body - rise / run * rear_y + 1
    on_cap = lambda shape: shape.rotate((0, 0, 0), (1, 0, 0), slope_angle).translate((0, 0, cap_z))
    body = cq.Workplane("YZ", origin=(-p["captain_width"] / 2, 0, 0)).polyline([
        (-rear_y, support_z), (rear_y, support_z),
        (rear_y, support_z + rear_body), (front_y, support_z + front_body)
    ]).close().extrude(p["captain_width"]).val()
    report["shoes"] = []
    for side in ("right", "left"):
        shoe = shapes["01_base_" + side]
        canonical = shoe if side == "right" else shoe.mirror("YZ")
        faces = []
        for face in canonical.Faces():
            if face.geomType() != "PLANE":
                continue
            normal = face.normalAt()
            if abs(normal.x) < 1e-6 and normal.z > .5 and abs(-normal.y / normal.z - expected_slope) < 1e-6:
                faces.append(face)
        assert faces, "Top wall plane not found: " + side
        face = max(faces, key=lambda item: item.Area())
        normal, point = face.normalAt(), face.Center()
        measured = [point.z - (y - point.y) * normal.y / normal.z - base_top for y in (-36, 36)]
        require_close(measured[0], expected_front, label="front wall height above shelf")
        require_close(measured[1], expected_rear, label="rear wall height above shelf")
        require_close(shoe.intersect(body).Volume(), 0, .01, "base/Captain interference")
        require_close(shoe.intersect(shapes["02_clamp_plate_" + side]).Volume(), 0, .01, "base/clamp plate interference")
        bores = []
        for local_y in (-10, 10):
            gauge = on_cap(cylinder(2.2, 110, (wall_x + 4, local_y, -100), (0, 0, 1)))
            retained = canonical.intersect(gauge).Volume()
            require_close(retained, 0, .01, "M4 hole passes through the entire base")
            head_top = p["cap_screw_normal_thickness"]
            screw = on_cap(cylinder(2, 30, (wall_x + 4, local_y, head_top - 30), (0, 0, 1)))
            require_close(screw.intersect(canonical).Volume(), 0, .01, "M4x30 screw/base interference")
            require_close(screw.intersect(body).Volume(), 0, .01, "M4x30 screw/Captain interference")
            assert screw.BoundingBox().zmin > base_top, "M4x30 screw tip below shelf"
            bores.append({"local_y_mm": local_y, "test_gauge_diameter_mm": 4.4,
                          "material_inside_through_gauge_mm3": round(retained, 9),
                          "nominal_screw_diameter_mm": 4, "countersunk_screw_total_length_mm": 30,
                          "screw_lower_envelope_above_floor_mm": screw.BoundingBox().zmin,
                          "screw_captain_distance_mm": screw.distance(body)})
            # The pocket opens from the side, with upper/lower retaining surfaces
            # and intact side walls; only the screw channel passes through.
            pocket = on_cap(box(wall_x + .05, local_y - 3.65, -17.95,
                                wall_thickness + .5, 7.3, 3.5))
            require_close(canonical.intersect(pocket).Volume(), 0, .01, "M4 nut pocket clearance")
            retained_bearings = []
            for local_z in (-18.2, -14.35):
                ring = cylinder(3.4, .15, (wall_x + 4, local_y, local_z), (0, 0, 1)).cut(
                    cylinder(2.3, .15, (wall_x + 4, local_y, local_z), (0, 0, 1)))
                bearing = on_cap(ring)
                require_equal_shape(canonical.intersect(bearing), bearing, "M4 nut retaining surface " + side)
                retained_bearings.append(round(bearing.Volume(), 6))
            for edge_y in (local_y - 3.9, local_y + 3.75):
                flank = on_cap(box(wall_x + .7, edge_y, -17.9, 6.6, .15, 3.4))
                require_equal_shape(canonical.intersect(flank), flank, "M4 nut anti-rotation side wall " + side)
            bores[-1]["nut_pocket_clear_gauge_mm"] = [7.3, 3.5]
            bores[-1]["nut_retaining_bearing_gauges_mm3"] = retained_bearings
            bores[-1]["nut_lateral_retention_present"] = True
        report["shoes"].append({"side": side, "wall_front_height_from_shelf_mm": measured[0],
            "wall_rear_height_from_shelf_mm": measured[1], "pivot_z_mm": p["base_pivot_z"],
            "wall_height_excludes_raised_joint_hub": True, "captain_interference_mm3": 0,
            "cap_interference_mm3": 0, "screw_through_checks": bores})

    report["clamping_plates"] = []
    normal_plate = p["cap_normal_thickness"]
    normal_boss = p["cap_screw_normal_thickness"]
    for side in ("right", "left"):
        cap = shapes["02_clamp_plate_" + side]
        if side == "left": cap = cap.mirror("YZ")
        cap = cap.translate((0, 0, -cap_z)).rotate((0, 0, 0), (1, 0, 0), -slope_angle)
        web = cap.intersect(box(p["captain_width"]/2-4, -.05, -1, .1, .1, 10))
        thickness = require_close(web.BoundingBox().zlen, normal_plate, label="plate thickness normal to the surface")
        cones = [face for face in cap.Faces() if face.geomType() == "CONE"]
        assert len(cones) == 2, "Two countersinks are required on clamp plate " + side
        for face in cones:
            require_close(face.BoundingBox().zlen, 2.35, label="countersink depth")
        for local_y in (-10, 10):
            boss_strip = cap.intersect(box(wall_x + 9, local_y-.05, -1, .1, .1, 10))
            require_close(boss_strip.BoundingBox().zlen, normal_boss, label="screw boss thickness normal to the surface")
            gauge = cylinder(2.2, 10, (wall_x+4, local_y, -1), (0, 0, 1))
            require_close(cap.intersect(gauge).Volume(), 0, .01, "clamp plate screw passage")
        report["clamping_plates"].append({"side": side, "normal_plate_thickness_mm": thickness,
            "normal_screw_boss_thickness_mm": normal_boss, "countersinks": 2,
            "countersink_depth_mm": 2.35})

    report["spacers"] = []
    for name in ("06_spacer_1", "06_spacer_2"):
        spacer = shapes[name]
        assert cylindrical_faces(spacer, 5, 1) and cylindrical_faces(spacer, 2.25, 1), "Spacer diameters: " + name
        require_close(spacer.BoundingBox().ylen, 6, label="spacer length")
        require_close(spacer.Volume(), math.pi*(5**2-2.25**2)*6, .001, "annular spacer volume")
        report["spacers"].append({"part": name, "outer_diameter_mm": 10,
                                 "bore_diameter_mm": 4.5, "length_mm": 6})

    report["knobs"] = []
    for level in ("lower", "upper"):
        for side in ("right", "left"):
            name = f"07_{level}_knob_{side}"
            knob = shapes[name]
            if side == "left": knob = knob.mirror("YZ")
            centers = {(round(point[1], 6), round(point[2], 6))
                       for _, point in cylindrical_faces(knob, 3.3, 0)}
            assert len(centers) == 1, "Knob center bore: " + name
            ky, kz = next(iter(centers))
            require_close(ky, p["base_pivot_y"], label="knob center Y")
            require_close(kz, p["base_pivot_z"] + (p["arm_length"] if level == "upper" else 0), label="knob center Z")
            require_close(knob.BoundingBox().xlen, p["knob_grip_thickness"], label="knob thickness")
            outer_x = knob.BoundingBox().xmax
            nut_af, nut_depth = p["knob_nut_af"], p["knob_nut_depth"]
            hex_gauge = cq.Workplane("YZ", origin=(outer_x-nut_depth+.05, ky, kz)).polygon(
                6, (nut_af-.1)/math.cos(math.pi/6)).extrude(nut_depth-.1).val()
            require_close(knob.intersect(hex_gauge).Volume(), 0, .01, "knob hexagonal pocket")
            support_ring = cylinder(4.5, .05, (outer_x-nut_depth-.1, ky, kz), (1, 0, 0)).cut(
                cylinder(3.5, .05, (outer_x-nut_depth-.1, ky, kz), (1, 0, 0)))
            require_equal_shape(knob.intersect(support_ring), support_ring, "knob nut retaining surface")
            report["knobs"].append({"part": name, "center_yz_mm": [ky, kz],
                "bore_diameter_mm": 6.6, "nut_pocket_af_mm": nut_af,
                "nut_pocket_depth_mm": nut_depth, "nut_retaining_floor_present": True})

    display_local = box(-p["display_width"] / 2, py + 4 - p["display_thickness"],
        pz - p["display_height"] / 2 - p["display_pivot_offset_z"],
        p["display_width"], p["display_thickness"], p["display_height"])
    arm_angle, display_angle = p["front_arm_deg"], p["front_display_deg"]
    display = pose_core("04_reference", display_local, arm_angle, display_angle, p, (py, pz))
    distance = body.distance(display)
    require_close(body.intersect(display).Volume(), 0, .01, "Captain/display interference")
    horizontal_gap = body.BoundingBox().ymin - display.BoundingBox().ymax
    assert 0 < horizontal_gap <= p["front_gap_max_mm"], f"Front horizontal gap: {horizontal_gap} mm"
    require_close(display.BoundingBox().zlen, p["display_thickness"], label="horizontal front display")
    screen_face = next(face for face in display_local.Faces() if face.normalAt().y < -.999)
    posed_screen_face = pose_core("04_reference", screen_face, arm_angle, display_angle, p, (py, pz))
    assert posed_screen_face.normalAt().z > .999999, "The screen glass must face upward"
    report["front_clearance"] = {"arm_deg": arm_angle, "display_deg": display_angle,
        "body_distance_mm": distance, "horizontal_gap_mm": horizontal_gap,
        "maximum_horizontal_gap_mm": p["front_gap_max_mm"],
        "floor_clearance_mm": display.BoundingBox().zmin,
        "plan_projection_gap_mm": horizontal_gap,
        "note": "The configured limit applies to horizontal clearance. The 3D distance also includes the vertical offset."}
    # Recalculate the critical transition from transport with temporarily
    # raised arms, using the actual waypoints supplied to the preview.
    scene = json.loads((out / "scene.json").read_text(encoding="utf-8"))
    path = scene["motion_paths"]["transport_to_front"]
    assert len(path) == 4, "The front transition path must contain four waypoints"
    assert path[1][0] > p["front_arm_deg"] and path[2][0] == path[1][0], "Temporary arm lift is missing"
    assert path[1][1] == p["folded_display_deg"] and path[2][1] == p["front_display_deg"]
    step = 360 / expected_teeth
    for name, angles in scene["poses"].items():
        assert all(abs(value / step - round(value / step)) < 1e-7 for value in angles), f"Pose cannot engage the 28-tooth joint: {name}"
    report["locked_pose_indexing"] = {"step_deg": step, "poses": scene["poses"], "all_indexed": True}
    states = []
    for (a0, d0), (a1, d1) in zip(path, path[1:]):
        n = max(1, math.ceil(max(abs(a1-a0), abs(d1-d0)) / 5))
        states.extend((a0 + (a1-a0)*i/n, d0 + (d1-d0)*i/n) for i in range(n+1))
    minimum_body, minimum_floor = float("inf"), float("inf")
    for a, d in states:
        moving_display = pose_core("04_reference", display_local, a, d, p, (py, pz))
        minimum_body = min(minimum_body, body.distance(moving_display))
        minimum_floor = min(minimum_floor, moving_display.BoundingBox().zmin)
        assert body.distance(moving_display) > .001, f"Display/Captain interference along the path: {a}, {d}"
        assert moving_display.BoundingBox().zmin > 0
        for name, shape in shapes.items():
            transformed = pose_core(name, shape, a, d, p, (py, pz))
            assert transformed.BoundingBox().zmin >= -.001
            assert transformed.intersect(body).Volume() < .01, f"Captain/part interference along the path: {name}, {a}, {d}"
    report["critical_front_path"] = {"waypoints": path, "samples": len(states), "step_max_deg": 5,
        "minimum_display_body_distance_mm": minimum_body,
        "minimum_display_floor_clearance_mm": minimum_floor, "collisions": 0}

    # Independently sample the horizontal arc against the body, reference switches,
    # cable corridor and printed parts reimported from STEP; the glass faces upward.
    switch_shapes = []
    lift = base_top + 1 - p["captain_original_foot_height"]
    mid_body = rear_body - rise / run * rear_y
    for x in (-116, -60, -4, 52, 114):
        for y in (-25, 25):
            top = p["captain_total_height"] + lift + rise / run * (y - 25)
            bottom = support_z + mid_body + rise / run * y
            switch = cylinder(9, 6, (x, y, top-6), (0, 0, 1)).fuse(
                cylinder(4, top-bottom-.3, (x, y, bottom+.3), (0, 0, 1)))
            switch_shapes.append((f"Switch_{x}_{y}", switch))
    cable = box(-p["captain_width"]/2, rear_y, support_z, p["captain_width"],
                p["cable_corridor_depth"], p["cable_corridor_top"]-support_z)
    h = scene["horizontal_motion"]
    require_close(h["display_deg"], 90, label="horizontal display in the animation")
    start, end = h["arm_min_deg"], h["arm_max_deg"]
    n = math.ceil(end-start)
    arc_states = [(start+(end-start)*i/n, 1.4) for i in range(n+1)]
    first, last = round(start/step), round(end/step)
    stops = [i*step for i in range(first, last+1)]
    assert len(stops) == 13, "There must be 13 horizontal stops with engaged joints"
    arc_states += [(value, 0) for value in stops]
    minima = {"body": float("inf"), "switch": float("inf"), "cable": float("inf"),
              "moving_floor": float("inf")}
    for index, (a, release) in enumerate(arc_states):
        display = pose_core("04_reference", display_local, a, 90, p, (py, pz))
        face = pose_core("04_reference", screen_face, a, 90, p, (py, pz))
        assert face.normalAt().z > .999999
        require_close(display.BoundingBox().zlen, p["display_thickness"], label="display plane throughout the arc")
        minima["body"] = min(minima["body"], display.distance(body))
        minima["cable"] = min(minima["cable"], display.distance(cable))
        minima["switch"] = min(minima["switch"], min(display.distance(ob) for _, ob in switch_shapes))
        transformed = []
        for name, shape in shapes.items():
            posed = pose_core(name, shape, a, 90, p, (py, pz))
            if release and name.startswith(("03_", "07_")):
                posed = posed.translate((release if name.endswith("right") else -release, 0, 0))
            assert posed.BoundingBox().zmin >= -.001
            if name.startswith(("03_", "04_", "06_", "07_")):
                minima["moving_floor"] = min(minima["moving_floor"], posed.BoundingBox().zmin)
            transformed.append((name, posed))
        for name, shape in transformed + [("display", display)]:
            for ob_name, ob in [("Captain", body), ("cables", cable)] + switch_shapes:
                assert not overlaps(shape, ob), f"Horizontal arc interference: {name}/{ob_name} at {a}"
        for i, (name, shape) in enumerate(transformed):
            assert not overlaps(shape, display), f"Display/part interference: {name} at {a}"
            for other_name, other in transformed[i+1:]:
                assert not overlaps(shape, other), f"Part/part interference along the arc: {name}/{other_name} at {a}"
        if index % 40 == 0:
            print(f"  Independent arc {index}/{len(arc_states)}", flush=True)
    report["horizontal_arc"] = {"waypoints": scene["motion_paths"]["front_to_rear_flat"],
        "moving_samples": n+1, "sample_max_deg": 1, "clamped_stops": stops,
        "display_surface_horizontal_and_facing_up": True, "minimum_clearances_mm": minima,
        "collisions": 0}
    report["limitations"] = ["Nominal geometric verification of exported files; strength and friction are not measured.",
        "The stock feet and actual equipment envelopes require confirmed dimensions.",
        "The complete motion path and UI are checked separately from this verifier."]
    report["status"] = "PASS"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "parameters.json")
    parser.add_argument("--output", type=Path, default=ROOT / "final_stand")
    args = parser.parse_args()
    p = json.loads(args.config.read_text(encoding="utf-8-sig"))
    report_path = args.output / "independent_report.json"
    try:
        report = verify(p, args.output)
    except Exception as error:
        args.output.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"status": "FAIL", "error": str(error),
            "traceback": traceback.format_exc()}, indent=2, ensure_ascii=False), encoding="utf-8")
        raise
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"PASS: {len(report['reimport'])} STEP/STL; dimensions, nut pockets, horizontal display and arc above the switches verified.", flush=True)
    print(report_path, flush=True)


if __name__ == "__main__":
    main()
