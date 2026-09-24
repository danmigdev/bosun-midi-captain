"""MIDI Captain STD stand geometry. Millimeters; X right, Y rear, Z up.

Parametric CadQuery solids. STEP files are editable solids; STL files are
print-oriented meshes. Equipment envelopes are dimensional references only.
"""
import math
import itertools
from functools import lru_cache
import cadquery as cq
import numpy as np
import trimesh



def captain_profile(p):
    rise = p["captain_rear_height_ground"] - p["captain_front_height_ground"]
    run = math.sqrt(p["captain_top_surface_depth"] ** 2 - rise ** 2)
    rear_y = p["captain_depth"] / 2
    front_y = rear_y - run
    slope = rise / run
    # Rear edges are assumed aligned; the stock feet require measurement.
    front_body = p["captain_front_height_ground"] - p["captain_original_foot_height"]
    rear_body = p["captain_rear_height_ground"] - p["captain_original_foot_height"]
    return dict(slope=slope, angle_deg=math.degrees(math.atan(slope)),
                top_run_mm=run, front_y=front_y, rear_y=rear_y,
                front_body=front_body, rear_body=rear_body,
                body_at_y0=rear_body - slope * rear_y,
                lift_mm=p["base_shelf_thickness"]+p["base_foot_height"]+1-p["captain_original_foot_height"])


def pivot_position(p, arm_angle):
    a = math.radians(arm_angle)
    return (p["base_pivot_y"] + p["arm_length"] * math.sin(a),
            p["base_pivot_z"] + p["arm_length"] * math.cos(a))


def joint_root_x(p):
    return max(p["captain_width"]/2+14,
               p["display_width"]/2+p["display_side_clearance"]+8)


def fold_waypoints(p):
    return [[p["arm_angle_deg"],p["tilt_deg"]],
            [p["arm_angle_deg"],-40], [0,-40], [0,p["folded_display_deg"]],
            [p["folded_arm_deg"],p["folded_display_deg"]]]


def box(x, y, z, dx, dy, dz):
    return cq.Workplane("XY").box(dx, dy, dz, centered=False).translate((x, y, z)).val()


def cylinder(r, length, p, direction):
    return cq.Solid.makeCylinder(r, length, cq.Vector(*p), cq.Vector(*direction))


def prism_yz(points, x, length):
    return cq.Workplane("YZ", origin=(x, 0, 0)).polyline(points).close().extrude(length).val()


def hex_x(x, y, z, af, depth):
    return cq.Workplane("YZ", origin=(x, y, z)).polygon(6, af / math.cos(math.pi / 6)).extrude(depth).val()


def hex_y(x, y, z, af, depth):
    return cq.Workplane(cq.Plane(origin=(x, y, z), xDir=(1, 0, 0), normal=(0, 1, 0))).polygon(6, af / math.cos(math.pi / 6)).extrude(depth).val()


def fused(*shapes):
    result = shapes[0]
    for shape in shapes[1:]:
        result = result.fuse(shape)
    return result.clean()


@lru_cache(maxsize=4)
def toothed_pair(teeth, height, clearance):
    """Male radial face gear and matching recess; X axis, root at X=0.

    Teeth occupy radii 9 to 14.5 mm with a triangular axial profile.
    The female surface is cut from the male profile. A screw holds engagement.
    """
    n = teeth * 2
    vertices = []
    for top in (True, False):
        for i in range(n):
            a = 2 * math.pi * i / n
            x = (height if i % 2 == 0 else 0) if top else -.1
            for radius in (9, 14.5):
                vertices.append((x, radius * math.cos(a), radius * math.sin(a)))
    triangles = []
    def quad(a, b, c, d):
        triangles.extend(((a, b, c), (a, c, d)))
    for i in range(n):
        j = (i + 1) % n
        a, b, c, d = 2*i, 2*i+1, 2*j+1, 2*j
        off = 2*n
        quad(a, b, c, d)
        quad(a+off, d+off, c+off, b+off)
        quad(b, b+off, c+off, c)
        quad(a, d, d+off, a+off)
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=True)
    mesh.fix_normals()
    assert mesh.is_volume, "Face gear mesh is not closed"
    faces = []
    for f in mesh.faces:
        points = [cq.Vector(*mesh.vertices[i]) for i in f]
        faces.append(cq.Face.makeFromWires(cq.Wire.makePolygon(points, close=True)))
    male = cq.Solid.makeSolid(cq.Shell.makeShell(faces))
    assert male.isValid(), "Invalid face gear solid"
    # Use the same polygon subdivision as the male ring to avoid tiny slivers
    # between the circle and the chords at the tooth boundaries.
    blank = cq.Workplane("YZ", origin=(clearance, 0, 0)).polygon(n, 29).polygon(n, 18).extrude(height + .1).val()
    female = blank.cut(male.translate((clearance, 0, 0))).clean()
    assert female.isValid(), "Invalid matching recess"
    return male, female


def mirror(shape):
    return shape.mirror("YZ")


def mesh_data(shape):
    vertices, faces = shape.tessellate(0.18, 0.18)
    return {"vertices": [[round(v.x, 4), round(v.y, 4), round(v.z, 4)] for v in vertices],
            "faces": [list(f) for f in faces]}


def make_model(p):
    half = p["captain_width"] / 2
    wall_x = half + 4
    root_x = joint_root_x(p)
    wall_thickness = root_x - wall_x
    wall_outer_x = wall_x+wall_thickness
    arm_x = root_x + p["joint_tooth_height"] + p["joint_axial_clearance"]
    ear_x = root_x - 8
    male, female = toothed_pair(p["joint_teeth"], p["joint_tooth_height"], p["joint_axial_clearance"])
    py, pz = p["pivot_y"], p["pivot_z"]
    parts = []
    refs = []
    profile = captain_profile(p)
    slope = profile["slope"]
    slope_angle = profile["angle_deg"]
    body_mid = profile["body_at_y0"]
    base_top = p["base_shelf_thickness"] + p["base_foot_height"]
    support_z = base_top + 1
    cap_z = support_z + body_mid + 1
    # Clamp thicknesses are measured normal to the contact face.
    cap_normal_thickness = p["cap_normal_thickness"]
    screw_normal_thickness = p["cap_screw_normal_thickness"]
    def on_cap(shape):
        return shape.rotate((0, 0, 0), (1, 0, 0), slope_angle).translate((0, 0, cap_z))

    def add(name, shape, group, color, print_axis="z", material="PA12-CF / PETG"):
        parts.append(dict(name=name, shape=shape.clean(), group=group, color=color,
                          print_axis=print_axis, material=material))

    # The base supports the chassis from below and leaves the connectors clear.
    # The shelf and pads place the chassis underside at the specified height.
    # The shelf extends beneath the entire outer wall.
    shelf_width = max(36, wall_outer_x-(half-22))
    base = fused(box(half - 22, -44, p["base_foot_height"], shelf_width, 84, p["base_shelf_thickness"]),
                 box(half - 22, -44, 0, shelf_width, 8, p["base_foot_height"]),
                 box(half - 22, 32, 0, shelf_width, 8, p["base_foot_height"]))
    # Vertical heights above the shelf surface at the Y +/-36 edges.
    # The circular pivot hub is added separately below.
    front_height = float(p["base_wall_front_height"])
    rear_height = float(p["base_wall_rear_height"])
    assert min(front_height, rear_height) > 0, "Wall heights must be positive"
    wall_profile = [(-36, base_top), (36, base_top),
                    (36, base_top + rear_height), (-36, base_top + front_height)]
    wall = prism_yz(wall_profile, wall_x, wall_thickness)
    rib_envelope = prism_yz(wall_profile, wall_x, max(10, wall_thickness))
    for y in (-10, 10):
        rib = on_cap(box(wall_x, y - 6, -70, 10, 12, 70))
        wall = fused(wall, rib.intersect(rib_envelope))
    shoe = fused(base, wall)
    by, bz = p["base_pivot_y"], p["base_pivot_z"]
    shoe = fused(shoe, cylinder(15, p["base_joint_disc_thickness"],
        (root_x-p["base_joint_disc_thickness"], by, bz), (1,0,0)),
        cylinder(8.5, root_x-wall_x, (wall_x, by, bz), (1,0,0)))
    shoe = shoe.cut(cylinder(3.3, root_x-wall_x+2, (wall_x - 1, by, bz), (1, 0, 0)))
    shoe = shoe.cut(hex_x(wall_x - .05, by, bz, 10.5, 4.5))
    shoe = fused(shoe, male.translate((root_x, by, bz)))
    for y in (-10, 10):
        bore_start = -100
        shoe = shoe.cut(on_cap(cylinder(2.25, 10 - bore_start,
                                       (wall_x + 4, y, bore_start), (0, 0, 1))))
        # Insert the M4 nut from the outer side before tightening the clamp plate.
        shoe = shoe.cut(on_cap(box(wall_x, y - 3.7, -18, max(11,wall_thickness+1), 7.4, 3.6)))
    assert 0 < cap_normal_thickness <= 5, "Upper clamp plates: maximum specified thickness is 5 mm"
    cap = box(half - 7, -18, 0, 21, 36, cap_normal_thickness)
    for y in (-10, 10):
        cap = fused(cap, cylinder(5.25, screw_normal_thickness, (wall_x+4,y,0),(0,0,1)))
        cap = cap.cut(cylinder(2.25, screw_normal_thickness + 2, (wall_x + 4, y, -1), (0, 0, 1)))
        cap = cap.cut(cq.Solid.makeCone(2.25,4.6,2.35,
            cq.Vector(wall_x+4,y,screw_normal_thickness-2.35),cq.Vector(0,0,1)))
    cap = on_cap(cap)
    for side, sh, ca in (("right", shoe, cap), ("left", mirror(shoe), mirror(cap))):
        add("01_base_" + side, sh, "fixed", "#304557")
        add("02_clamp_plate_" + side, ca, "fixed", "#4b677b")
        parts[-1]["flatten_slope"] = slope_angle

    # Two cantilever arms without rear supports. The reference geometry is
    # vertical; pose() applies the angle at the base pivot.
    arm = fused(box(arm_x+p["arm_disc_thickness"]-p["arm_web_thickness"], by - 8, bz,
                    p["arm_web_thickness"], 16, p["arm_length"]),
                cylinder(15, p["arm_disc_thickness"], (arm_x, by, bz), (1, 0, 0)),
                cylinder(15, p["arm_disc_thickness"], (arm_x, by, bz + p["arm_length"]), (1, 0, 0)))
    for z in (bz, bz + p["arm_length"]):
        arm = arm.cut(cylinder(3.3, 10, (arm_x - 1, by, z), (1, 0, 0)))
        arm = fused(arm, female.translate((root_x, by, z)))
    for side, shape in (("right", arm), ("left", mirror(arm))):
        add("03_arm_" + side, shape, "arm", "#4b677b", "x" if side=="right" else "x_up")

    # Hand knobs have an outward-facing hexagonal nut pocket.
    # The body is 5 mm thick; a 3.2 mm low-profile M6 nut sits in a 3.4 mm pocket.
    # Leave 1.6 mm for a metal washer between the arm and knob.
    knob_x = arm_x + p["arm_disc_thickness"] + 1.6
    for level, z in (("lower", bz), ("upper", bz + p["arm_length"])):
        knob = fused(cylinder(10.5,p["knob_grip_thickness"],(knob_x,by,z),(1,0,0)),
                     cylinder(8,p["knob_hub_thickness"],(knob_x,by,z),(1,0,0)))
        for i in range(6):
            a = i * math.pi / 3
            knob = fused(knob, cylinder(3.5, p["knob_grip_thickness"],
                (knob_x, by + 10.5 * math.cos(a), z + 10.5 * math.sin(a)), (1, 0, 0)))
        knob = knob.cut(cylinder(3.3, 12, (knob_x - 1, by, z), (1, 0, 0)))
        knob = knob.cut(hex_x(knob_x + p["knob_hub_thickness"] - p["knob_nut_depth"], by, z,
                             p["knob_nut_af"], p["knob_nut_depth"] + .2))
        for side, shape in (("right", knob), ("left", mirror(knob))):
            add("07_" + level + "_knob_" + side, shape, "arm", "#d9aa78", "x_up" if side=="right" else "x")

    # Moving parts are modeled around the local origin, then placed at the pivot.
    pitch_x = p["display_hole_pitch_x"] / 2
    pitch_z = p["display_hole_pitch_z"] / 2
    hx, hz = p["display_hole_offset_x"], p["display_hole_offset_z"]
    offset = p["display_pivot_offset_z"]
    central_half = max(49.5, pitch_x + abs(hx) + 12)
    display_top = p["display_height"] / 2 - offset
    # The band and circular ears align with the top edge of the display.
    # The mounting holes follow the display; the pivot is 16 mm below its top edge.
    band_thickness = p["display_band_thickness"]
    boss_thickness = p["display_boss_thickness"]
    panel = box(-central_half, 12, display_top-p["display_mount_band"],
                central_half * 2, band_thickness, p["display_mount_band"])
    beam = box(-ear_x - 2, 12, display_top-16,
               2 * (ear_x + 2), band_thickness, 16)
    # Mounting bosses start at the spacer seat at Y=10;
    # the front band face is at Y=12.
    for x, z in itertools.product((-pitch_x, pitch_x), (pitch_z,)):
        panel = fused(panel, cylinder(6.5, boss_thickness,
                                     (x + hx, 10, z + hz-offset), (0, 1, 0)))
    ear = cylinder(15, 8, (ear_x, 0, 0), (1, 0, 0))
    ear = ear.cut(cylinder(3.3, 10, (ear_x - 1, 0, 0), (1, 0, 0)))
    ear = ear.cut(hex_x(ear_x - .05, 0, 0, 10.5, 4.5))
    ear = fused(ear, male.translate((root_x, 0, 0)))
    full_panel = fused(panel, beam, ear, mirror(ear))
    # Through openings preserve the mounting holes and arm connections.
    for x, z in itertools.product((-pitch_x, pitch_x), (pitch_z,)):
        full_panel = full_panel.cut(cylinder(p["display_clearance_hole"] / 2,
                                            max(boss_thickness+2, band_thickness+4),
                                            (x + hx, 9, z + hz-offset), (0, 1, 0)))
    add("04_display_crossbar", full_panel.translate((0, py, pz)),
        "moving", "#de9255", "y")

    # Spacers provide airflow and room for the central mounting hardware.
    for i, (x, z) in enumerate(itertools.product((-pitch_x, pitch_x), (pitch_z,)), 1):
        pos = (x + hx, py + 4, pz + z + hz-offset)
        spacer = cylinder(5, 6, pos, (0, 1, 0)).cut(cylinder(2.25, 6, pos, (0, 1, 0)))
        add(f"06_spacer_{i}", spacer, "moving", "#5c6972", "y")

    depth = p["captain_depth"] / 2
    body = prism_yz([(-depth, support_z), (depth, support_z),
        (profile["rear_y"], support_z+profile["rear_body"]),
        (profile["front_y"], support_z+profile["front_body"])], -half, p["captain_width"])
    refs.append(dict(name="Captain_nominal_envelope", shape=body, group="reference_fixed", color="#161f2a"))
    for x in (-116, -60, -4, 52, 114):
        for y in (-25, 25):
            top = profile["lift_mm"] + p["captain_total_height"] + slope * (y - 25)
            bottom = support_z + body_mid + slope * y
            switch = fused(cylinder(9, 6, (x, y, top - 6), (0, 0, 1)),
                           cylinder(4, top - bottom - .3, (x, y, bottom + .3), (0, 0, 1)))
            refs.append(dict(name=f"Switch_reference_{x}_{y}", shape=switch, group="reference_fixed", color="#bbc5cf"))
    refs.append(dict(name="Captain_LCD_reference", shape=box(-46, -10, 0, 28, 20, .6).rotate((0,0,0),(1,0,0),slope_angle).translate((0,0,support_z+body_mid)), group="reference_fixed", color="#4cafac"))
    refs.append(dict(name="Encoder_reference", shape=cylinder(7, 8, (20, 0, 0), (0, 0, 1)).rotate((0,0,0),(1,0,0),slope_angle).translate((0,0,support_z+body_mid)), group="reference_fixed", color="#667380"))
    disp = box(-p["display_width"] / 2, py + 4 - p["display_thickness"], pz - p["display_height"] / 2-offset,
               p["display_width"], p["display_thickness"], p["display_height"])
    refs.append(dict(name="Display_nominal_envelope", shape=disp, group="reference_moving", color="#101a22"))
    screen = box(-p["display_width"] / 2 + 5, py + 3.5 - p["display_thickness"], pz - p["display_height"] / 2 + 4-offset,
                 p["display_width"] - 10, .5, p["display_height"] - 8)
    refs.append(dict(name="Display_screen_reference", shape=screen, group="reference_moving", color="#3eacaa"))
    return parts, refs


def tilt(shape, angle, p):
    return shape.rotate((0, p["pivot_y"], p["pivot_z"]), (1, p["pivot_y"], p["pivot_z"]), -angle)


def pose(part, arm_angle, screen_angle, p):
    sh = part["shape"]
    if part["group"] == "arm":
        return sh.rotate((0, p["base_pivot_y"], p["base_pivot_z"]),
                         (1, p["base_pivot_y"], p["base_pivot_z"]), -arm_angle)
    py, pz = pivot_position(p, arm_angle)
    shift = (0, py - p["pivot_y"], pz - p["pivot_z"])
    if part["group"].endswith("moving"):
        return tilt(sh, screen_angle, p).translate(shift)
    return sh


def overlaps(a, b):
    aa, bb = a.BoundingBox(), b.BoundingBox()
    if any(getattr(aa, k + "max") <= getattr(bb, k + "min") + .001 or
           getattr(bb, k + "max") <= getattr(aa, k + "min") + .001 for k in "xyz"):
        return False
    return a.intersect(b).Volume() > .01


def fold_pose(part, arm_angle, screen_angle, release, p):
    sh = pose(part, arm_angle, screen_angle, p)
    if part["group"] == "arm" and release:
        sh = sh.translate((release if part["name"].endswith("right") else -release, 0, 0))
    return sh


def print_shape(part):
    sh = part["shape"]
    if "flatten_slope" in part:
        sh = sh.rotate((0, 0, 0), (1, 0, 0), -part["flatten_slope"])
    if part["print_axis"] == "x":
        sh = sh.rotate((0, 0, 0), (0, 1, 0), 90)
    elif part["print_axis"] == "x_up":
        sh = sh.rotate((0, 0, 0), (0, 1, 0), -90)
    elif part["print_axis"] == "y":
        sh = sh.rotate((0, 0, 0), (1, 0, 0), -90)
    b = sh.BoundingBox()
    return sh.translate((-b.xmin, -b.ymin, -b.zmin))


def validate(parts, refs, p):
    checks = []
    for part in parts:
        shape = part["shape"]
        assert shape.isValid() and len(shape.Solids()) == 1, part["name"] + ": invalid solid"
        sh = print_shape(part)
        bounds = sh.BoundingBox()
        size = [bounds.xlen, bounds.ylen, bounds.zlen]
        fits_bed = all(v <= limit + .01 for v, limit in zip(size, p["printer_bed"]))
        assert fits_bed or part["name"] in p.get("allow_oversize_parts", []), (part["name"], size)
        checks.append({"part": part["name"], "solid_valid": True, "solids": 1,
                       "fits_configured_printer_bed": fits_bed,
                       "print_mm": [round(v, 2) for v in size], "volume_cm3": round(sh.Volume() / 1000, 2)})
    # Measure top alignment and a maximum 25 mm support band behind the display.
    display = next(v["shape"] for v in refs if v["group"] == "reference_moving")
    display_top = display.BoundingBox().zmax
    brackets = [v["shape"] for v in parts if v["name"].startswith("04_display_")]
    behind_display = box(-p["display_width"]/2, p["pivot_y"]-30, p["pivot_z"]-100,
                         p["display_width"], 60, 200)
    top_errors = [abs(v.BoundingBox().zmax-display_top) for v in brackets]
    band_depth = max(display_top-v.intersect(behind_display).BoundingBox().zmin for v in brackets)
    assert max(top_errors) < .001, "Bracket extends above the display top edge"
    assert band_depth <= 25.001, "Bracket extends beyond the upper 25 mm band"
    assert joint_root_x(p)-8-p["display_width"]/2 >= p["display_side_clearance"]-.001
    # Reserve the full cable corridor without assuming individual connector positions.
    support_z = p["base_shelf_thickness"]+p["base_foot_height"]+1
    cable = box(-p["captain_width"] / 2, p["captain_depth"] / 2, support_z,
                p["captain_width"], p["cable_corridor_depth"], p["cable_corridor_top"] - support_z)
    feet = box(-p["foot_clearance_half_width"], -95, 13,
               2 * p["foot_clearance_half_width"], p["foot_clearance_rear_y"] + 95, 210)
    body = refs[0]["shape"]
    index_step = 360.0 / p["joint_teeth"]
    def indexed_angle(angle):
        return abs(angle / index_step - round(angle / index_step)) < 1e-7
    def indexed_range(minimum, maximum):
        first = math.ceil(minimum / index_step - 1e-7)
        last = math.floor(maximum / index_step + 1e-7)
        return [i * index_step for i in range(first, last + 1)]
    assert indexed_angle(p["arm_angle_deg"]) and indexed_angle(p["tilt_deg"])
    angles = indexed_range(p["tilt_min"], p["tilt_max"])
    if not any(math.isclose(p["tilt_deg"], angle, abs_tol=1e-7) for angle in angles):
        angles.append(p["tilt_deg"])
    envelope, excluded, failures = [], [], []
    states = 0
    arm_angles = indexed_range(p["arm_angle_min"], p["arm_angle_max"])
    assert any(math.isclose(p["arm_angle_deg"], angle, abs_tol=1e-7) for angle in arm_angles)
    for arm_angle in arm_angles:
        allowed = []
        for angle in angles:
            printable = [(v["name"], pose(v, arm_angle, float(angle), p)) for v in parts]
            display_refs = [(v["name"], pose(v, arm_angle, float(angle), p))
                            for v in refs if v["group"] == "reference_moving"]
            problems = []
            for name, sh in printable + display_refs:
                if sh.BoundingBox().zmin < -.01:
                    problems.append(f"{name}: floor")
                for zone_name, zone in (("cables", cable), ("foot", feet), ("body", body)):
                    if overlaps(sh, zone):
                        problems.append(f"{name}: {zone_name}")
            for (a, sa), (b, sb) in itertools.combinations(printable, 2):
                if overlaps(sa, sb):
                    problems.append(f"{a} / {b}")
            if problems:
                excluded.append({"arm_deg": arm_angle, "tilt_deg": float(angle), "reasons": problems})
                if math.isclose(arm_angle, p["arm_angle_deg"], abs_tol=1e-7) and math.isclose(angle, p["tilt_deg"], abs_tol=1e-7):
                    failures.extend(problems)
            else:
                allowed.append(float(angle))
                states += 1
        assert allowed and all(abs(b - a - index_step) < 1e-7 for a, b in zip(sorted(allowed), sorted(allowed)[1:])), (arm_angle, allowed)
        envelope.append({"arm_deg": arm_angle, "tilt_min": min(allowed), "tilt_max": max(allowed)})
    assert not failures, "Invalid initial position: " + ", ".join(failures)
    # Check that off-index teeth prevent rotation and that axial separation
    # allows the joint to move freely.
    male, female = toothed_pair(p["joint_teeth"], p["joint_tooth_height"], p["joint_axial_clearance"])
    aligned = male.intersect(female).Volume()
    indexed = male.intersect(female.rotate((0,0,0), (1,0,0), index_step)).Volume()
    half_index = male.intersect(female.rotate((0,0,0), (1,0,0), index_step / 2)).Volume()
    released = male.intersect(female.translate((p["joint_tooth_height"] + .2,0,0)).rotate((0,0,0), (1,0,0), index_step / 2)).Volume()
    assert aligned < .01 and indexed < .01 and half_index > 1 and released < .01
    # Test a metal nut with 10 mm across flats and 3.2 mm height, including
    # outside insertion and anti-rotation engagement in all four knobs.
    knob_checks = []
    knob_x = joint_root_x(p) + p["joint_tooth_height"] + p["joint_axial_clearance"] + p["arm_disc_thickness"] + 1.6
    for part in parts:
        if not part["name"].startswith("07_"):
            continue
        z = p["base_pivot_z"] + (p["arm_length"] if "upper" in part["name"] else 0)
        y = p["base_pivot_y"]
        nut_x = knob_x + p["knob_hub_thickness"] - p["knob_nut_depth"]
        nut = hex_x(nut_x, y, z, 10, p["knob_nut_height"])
        nut = nut.cut(cylinder(3, 7, (nut_x-1, y, z), (1,0,0)))
        off_angle = nut.rotate((0,y,z),(1,y,z),30)
        for travel in np.linspace(0,8,17):
            test = nut.translate((float(travel),0,0))
            if part["name"].endswith("left"):
                test = mirror(test)
            assert not overlaps(part["shape"], test), part["name"] + ": nut cannot be inserted"
        if part["name"].endswith("left"):
            off_angle = mirror(off_angle)
        blocked = part["shape"].intersect(off_angle).Volume()
        assert blocked > 1, part["name"] + ": nut can rotate freely"
        knob_checks.append({"part": part["name"], "nut_insertion_clear": True,
                            "insertion_samples": 17, "rotation_30deg_interference_mm3": blocked})
    return {"status": "PASS nominal geometry; physical fit and strength have not been tested",
            "angles_checked_deg": [float(a) for a in sorted(angles)], "parts": checks,
            "valid_configurations": states, "clearance_envelope": envelope,
            "knob_nut_pocket": {"across_flats_mm": p["knob_nut_af"], "depth_mm": p["knob_nut_depth"],
                                "entry": "outer face", "checks": knob_checks},
            "display_support": {"side_clearance_mm": joint_root_x(p)-8-p["display_width"]/2,
                                "max_depth_below_top_mm": max(display_top-v["shape"].BoundingBox().zmin for v in parts if v["group"]=="moving"),
                                "band_depth_behind_display_mm": band_depth,
                                "top_edge_mismatch_mm": max(top_errors),
                                "hinge_below_display_top_mm": display_top-p["pivot_z"],
                                "note": "The rear support band is within 25 mm of the display top. Circular 30 mm ears are outside the 20 mm side clearance.",
                                "mounting_holes_used": "upper pair, 75 mm center spacing"},
            "excluded_configurations": excluded,
            "teeth": {"count": p["joint_teeth"], "step_deg": index_step,
                      "aligned_overlap_mm3": aligned, "next_index_overlap_mm3": indexed,
                      "blocked_half_index_overlap_mm3": half_index, "released_overlap_mm3": released},
            "cable_corridor_mm": [p["captain_width"], p["cable_corridor_depth"], p["cable_corridor_top"] - support_z],
            "foot_zone_note": "X +/-133, Y -95..70, Z 13..223. This reference zone does not cover every shoe or foot movement.",
            "limitations": p["unconfirmed"]}
