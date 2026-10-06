# Spline Basics Cheat-Sheet

Extracted from Spline's official docs (docs.spline.design), aimed at the specific
confusion we hit while positioning the ribbon's fold flap: typing a big change into
Position Y and watching nothing move.

## The likely cause: Local vs. World space

Spline's transform gizmo can work in two modes:

- **World space** — X/Y/Z always point the same fixed directions in the scene,
  no matter how the object itself is rotated.
- **Local space** — X/Y/Z are relative to the *object's own* rotation. If the object
  is rotated, "Y" might not mean "up" anymore.

Press **`L`** to toggle between them. If a number change isn't doing what you expect,
try toggling this before assuming you did something wrong.

## Essential shortcuts (Windows)

| Key | What it does |
|---|---|
| `Alt+R` | Reset View/Camera — snaps back to a known, predictable angle. Use this anytime you feel lost in the viewport. |
| `M` | Toggle Perspective / Orthographic |
| `Alt + drag` | Orbit the camera around the scene |
| `Space + drag` | Pan (slide the view without rotating) |
| `Ctrl + scroll wheel` | Zoom |
| `S` | Focus Object — selects and centers the chosen object in view |
| `Shift+S` | Orient camera to object |
| `L` | Switch transform gizmo between Local/World space |
| `Ctrl+D` | Duplicate |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / Redo |
| `Ctrl+G` | Group selection |
| Delete | Delete selected object |

## Practical habit going forward

When positioning something and it's not behaving as expected:

1. Press `Alt+R` to get to a known camera state first.
2. Press `L` to check/toggle Local vs. World space on the gizmo.
3. Make small changes and screenshot often, rather than large blind jumps.

Source: [Spline documentation](https://docs.spline.design/basics/keyboard-shortcuts)
