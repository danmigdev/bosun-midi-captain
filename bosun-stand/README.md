# Bosun Stand

This stand was designed around the [display listed on Amazon under ASIN B0FPR3GS11](https://www.amazon.it/dp/B0FPR3GS11).

Bosun Stand supports independent adjustment of the arms and display. The preview has separate continuous controls for **Arm position** and **Display rotation**, both ranging from **−180° to +180°**; either control keeps the other angle unchanged. An optional animation keeps the display horizontal while the arms move it from the front toward the rear of the MIDI Captain. The arms have **106 mm between pivot centres**, with **6.19 mm of horizontal clearance** between the display and the Captain's front edge in the front-level position.

Drag the 3D view with the mouse or one finger to rotate through **360° horizontally and vertically**, including underneath the stand. View rotation is independent of the arm and display controls.

- [Interactive 3D preview](final_stand/preview_3d.html).
- [All 13 printable parts](final_stand/stl).
- [Print package: all 13 STL files](Bosun_Stand_Print_Files.zip).
- [Arm STL files](Bosun_Stand_Final_2_Arms.zip).
- [Dimensions, assembly and verification](final_stand/README.md).
- [Complete final package](Bosun_Stand_Final_Complete.zip).

The design uses 28 evenly spaced teeth at each mating joint surface, base walls 22/32 mm high, and through holes for M4 countersunk screws with retained nuts. The preview has a light grey background. Manual angles remain free when a slider is released; use **Snap to teeth** explicitly to select locking angles. The contact indicator flags possible interference in custom positions using approximate envelopes. On the physical stand, loosen the four joint knobs, separate the teeth and support the display during adjustment. Left and right arms move together.

Release **1.0** includes the CAD sources, printable parts, assembled STEP models, interactive preview and verification reports. The `.venv` directory contains the local Python environment.

## Regeneration

Run these commands from the project directory:

```powershell
.\.venv\Scripts\python.exe -B generate_stand.py
.\.venv\Scripts\python.exe -B verify_stand.py
.\.venv\Scripts\python.exe -B build_preview.py --verify-browser
.\.venv\Scripts\python.exe -B package_stand.py
```

The verifier reads the exported files. The preview works locally without a network connection. See the [design notes](final_stand/README.md) for physical testing limits and the local CadQuery exit-code issue.
