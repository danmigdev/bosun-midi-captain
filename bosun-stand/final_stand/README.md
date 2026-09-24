# Bosun Stand

**Design reference:** this stand was designed around the [display listed on Amazon under ASIN B0FPR3GS11](https://www.amazon.it/dp/B0FPR3GS11).

Release **1.0**. The arms and display can rotate independently with their joints released. The interactive preview provides separate continuous controls for both angles, plus an optional animation that keeps the display parallel to the ground as the arms move it from the front toward the rear of the MIDI Captain. It uses a light grey background.

## Dimensions

| Component | Specification |
|---|---|
| Front base wall | **22 mm** above the upper surface of the shelf that slides under the Captain. |
| Rear base wall | **32 mm**, measured from the same shelf surface. |
| Clamp screws | **4.5 mm through holes** for M4 countersunk screws, including 30 mm screws, with captive nut pockets and retaining shoulders. |
| Side arms | **106 mm pivot spacing**, 136 mm overall length; 4 mm web thickness and 5 mm joint discs. |
| Front display clearance | **6.19 mm horizontally**, between the display edge and the Captain's front edge. |
| Joint teeth | **28 evenly spaced teeth on all eight mating surfaces**, giving steps of approximately **12.86°** at the four joints. |
| Display crossbar | One piece, with a 5 mm rear band, 7 mm mounting bosses and round **30 mm diameter × 8 mm** joint ends. |
| Upper clamp plates | **2.97 mm body thickness** and **4.95 mm at the screw bosses**, measured normal to the contact face, with countersunk M4 screw seats. |

Wall heights are measured vertically at the ends of the 72 mm wall. The top of the shelf is 3 mm above the floor, so the wall edges are 25 and 35 mm above the floor. The circular hinge hub extends above the wall profile.

The 6.19 mm front clearance is measured in plan view. The shortest distance between the two housings is **9.35 mm**, because their edges are also offset vertically. The distance changes during movement as the display rises above the Captain along the arm's arc.

## Preview and adjustment

Open `preview_3d.html` in a browser. It works without a network connection. The **Arm position** slider moves both arms together from **−180° to +180°** while preserving the display's orientation relative to the ground. The **Display rotation** slider rotates the display from **−180° to +180°** while preserving the arm angle and the display pivot position. These controls are available in every preset. The **Side** view shows both movements clearly.

Drag the 3D view with the mouse or one finger to rotate continuously through **360° horizontally and vertically**, including above and underneath the stand. The view passes smoothly over the top and bottom without stopping or flipping abruptly. Use the mouse wheel to zoom, or the **Perspective**, **Rear**, **Side** and **Top** buttons to reset the viewing orientation. Rotating the view does not change the arm or display angles.

Releasing either slider preserves its exact angle and leaves the joints disengaged. Use **Snap to teeth** explicitly to select the nearest indexed arm and display angles and show the joints engaged. The 28-tooth hardware locks at **12.86°** intervals: both the arm angle and the display angle relative to the arm must match the tooth spacing. Continuous adjustment requires the teeth to be separated.

The front position uses arm angle **−90°** and display angle **90°** in the model's coordinate system. A display angle of 90° is horizontal with the screen facing up. The separate **Optional level movement** control follows the checked horizontal path from arm angle −90° to **+64.29°**, with **13 locking positions** along that arc. From a custom position outside that arc, it first selects the front-level preset without simulating a transition.

The design does not level the display automatically. Loosen the four joint knobs, separate the teeth and support the display by hand during adjustment. Keeping the display at a constant angle while moving the arms requires compensating at the upper joints. The model uses 1.4 mm of axial separation to disengage the teeth. Do not force rotation while the teeth are engaged.

The manual control ranges include positions that can contact the Captain, switches, floor or reserved cable space. The preview keeps your requested angles and displays a **Possible contact** indicator when its approximate checks find interference. Selecting a preset from a custom position changes the view immediately; it does not imply that the direct movement is clear. Animations between named presets follow the checked CAD paths.

The rear limit of horizontal travel preserves the reserved cable space. To enter the tilted rear position, the animation raises the arms, rotates the display and returns it to the rear along a separate checked path. To move between transport and the front position, it briefly raises the arms before rotating the display.

The nominal tilted rear position uses arm angle 77.14° and display angle 12.86°. Other checked rear settings are listed under `clearance_envelope` in `geometry_report.json`.

## Printing and assembly

The complete assembly has **13 printed parts**. All STL files are in millimetres and oriented for printing; do not rescale them.

`Bosun_Stand_Print_Files.zip` contains the complete set of printable parts:

- Two bases with 22/32 mm walls and through holes.
- Two upper clamp plates with countersunk screw seats.
- Two arms with 106 mm pivot spacing.
- One display crossbar with round joint ends.
- Two display spacers.
- Four hand knobs.

`Bosun_Stand_Final_2_Arms.zip` contains the pair of arm STL files. Every mating toothed surface uses the same 28-tooth pattern. The external hand knobs do not have mating teeth.

The arms fit the specified 180 mm print bed. The one-piece crossbar is approximately 325 mm wide and requires a suitable printer. Check brim, supports and excluded bed areas in the slicer. The hinges use M6 fasteners and nut seats. The upper clamps use M4 screws through the bases.

## Verification and limits

`geometry_report.json`, `independent_report.json` and `browser_report.json` record the CAD, exported-file and browser checks. Verification covers solids, holes, part interference, nominal Captain/display envelopes, floor clearance, switch envelopes and the reserved rear cable space.

The horizontal path is checked at steps no greater than **0.5°**, together with all 13 locking positions. The full set of paths contains **755 sampled configurations**. An independent check reopens all 13 STEP and STL files and checks the horizontal arc at another 156 samples plus the 13 locked positions. The screen remains horizontal and faces upward, with no nominal collisions.

During horizontal travel, the minimum display clearance is **19.69 mm** from the indicated switches and **6.99 mm** from the reserved cable space. Moving printed parts remain at least **20 mm** above the floor during that horizontal arc. All eight toothed surfaces have 28 evenly spaced teeth. The 22/32 mm walls and M4×30 screw clearances are checked directly from the final solids.

Browser checks cover full camera rotation, movement through both vertical poles, both independent sliders, fractional angles retained after release, explicit tooth-position snapping, keyboard control, all preset transitions, level movement, contact indicators, background colour and mobile layout.

The free-angle contact indicator compares conservative convex projections of meshes clipped to overlapping width ranges, and checks moving parts against the floor. It includes the Captain, switch and encoder envelopes, bases, clamp plates and reserved cable space. Intended base/arm gear contacts and contacts between moving parts are excluded. This can report possible contacts where a detailed solid check would find clearance; no detected contact is not a guarantee for an arbitrary angle combination. The CAD verification of the specified presets and paths remains separate from these approximate interactive checks.

These are nominal geometry checks. Confirm print tolerances, actual connectors and cables, access to the switches with a shoe, joint grip and printed-part strength on the physical assembly. No physical load test has been performed.

## Files and regeneration

- `stl/`: all 13 final printable parts.
- `step/`: individual solid parts in modelling coordinates.
- `stand_front_with_references.step`, `stand_rear_flat_with_references.step`, `stand_rear_with_references.step`, `stand_transport_with_references.step`: four assemblies with reference envelopes; these are not printable-part files.
- `scene.json`, `preview_3d.html`, `images/`: scene data, interactive preview and screenshots.
- `sources/`, `manifest.json`: source files and SHA-256 file hashes.

Run from the project directory:

```powershell
.\.venv\Scripts\python.exe -B generate_stand.py
.\.venv\Scripts\python.exe -B verify_stand.py
.\.venv\Scripts\python.exe -B build_preview.py --verify-browser
.\.venv\Scripts\python.exe -B package_stand.py
```

The independent verifier needs only the final files. The local `.venv` Python environment is retained for regeneration. When using the extracted complete ZIP on another machine, treat its `sources/` directory as the project directory, create a Python 3.12 virtual environment there, and install `requirements.txt`. The commands above then generate a new `final_stand/` directory inside it. The browser check expects Chrome at its standard Windows installation path.

CadQuery on this machine can return exit code 1 during process shutdown after completing exports without a traceback; output files are checked and reimported in a separate process. Actual assertion failures and tracebacks must still be investigated.
