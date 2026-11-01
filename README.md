# Set Driven Key

**Maya-style Set Driven Key for Blender** — drive object or bone channels from a controller, with an optional **additive** mode so offsets stack on top of your own animation.

**Addon name (in Blender):** Set Driven Key  
**Version:** 1.1.0  
**Minimum Blender:** 3.0.0  
**Authors:** Oceanman, CloudKing  
**Location:** 3D Viewport → Sidebar (**N**) → **Driven Key**  
**Category:** Animation

---

## Overview

In Maya, **Set Driven Key** links one attribute (the *driver*) to others (the *driven*) so changing the driver value moves the driven channels through authored poses. Blender can do this with drivers and Action constraints, but the setup is manual and tedious.

This add-on provides a focused UI to:

1. Load a **driver** (object or pose bone + channel)
2. Load **driven** targets (objects or bones)
3. Enable the channels to drive (location / rotation / scale / quaternion / custom paths)
4. Pose and press **Key**

Under the hood it creates **driver F-curves** keyed by the driver value (not by time). It only manages curves that use the add-on’s own driver variable (`sdk_driver`), so hand-made drivers are left alone.

---

## Features

### Two modes

| Mode | Behaviour |
|------|-----------|
| **Additive** (default) | Non-destructive. Offsets stack on top of the base transform. You can still keyframe, pose, and animate the same channels. Objects use **delta transforms**; pose bones use **Transformation constraints** (`SDK Location` / `SDK Rotation` / `SDK Scale`) with driven min/max. |
| **Direct** | Maya-style. The driver takes over the channel and sets its value directly. |

### Driver

- Load the **active object**, or the **active pose bone** in Pose Mode
- Channels: Location XYZ, Rotation Euler XYZ, Quaternion WXYZ, Scale XYZ, or a **custom data path**
- Custom paths support RNA attributes and ID properties, e.g. `["myprop"]`, `location[2]`, `modifiers["Bevel"].width`
- **Rest value** — driver value at which driven channels are in their original state (auto-captured on load / channel change; editable)
- **Capture Driver Rest** / **Driver to Rest** helpers

### Driven targets

- Load **selected objects**, or **selected pose bones** in Pose Mode
- **Load Driven** replaces the list; **Add** appends without clearing
- Per-channel toggles: Location, Rotation (Euler), Quaternion, Scale
- **Custom** driven paths (semicolon-separated); custom paths are always Direct
- Base pose is captured on load for Additive offset math

### Keying

- **Key** — store enabled driven channels against the **current driver value** (creates drivers/curves if needed)
- **Auto Rest Key** (default on) — when a curve gets its first key, also key the rest state so channels return to base when the driver does
- **Reset to Base after Key** (Additive) — after keying, put posed channels back to the captured base; the on-screen pose stays the same because the offset now provides it
- **Interpolation:** Smooth (Bezier), Linear, Stepped (Constant)
- **Extrapolation:** Constant or Linear outside the keyed range

### Editing & cleanup

- **Delete Key at Driver Value** — remove keys at the current driver value from enabled channels
- **Delete Driven Curves** — remove SDK-managed drivers; reset additive outputs and unused SDK constraints
- **Live Preview** — toggle SDK effect on/off so you can see and pose raw base channels
- **Capture Base** / **Reset to Base** (Additive) — re-snapshot or restore the base pose

### Safety

- Does not overwrite drivers that were not created by this add-on
- Refuses self-drive (same object/path or same bone driving itself)
- Quaternion channels are Direct-only; Additive rotation on objects requires Euler mode
- Axis-angle rotation is not supported

---

## Installation

1. Download `set_driven_key.py` (or the repository ZIP).
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk** (or Install).
3. Select the file and enable **Set Driven Key**.
4. Open the 3D Viewport sidebar (**N**) → **Driven Key**.

**Requirements:** Blender **3.0** or newer.

---

## Quick start

### Basic workflow

1. Select the controller object (or pose bone) → **Load Driver**.
2. Choose the driver **Channel** (e.g. Location Y).
3. Select the objects or bones to control → **Load Driven**.
4. Enable the channels you want (e.g. Location X/Y/Z, Rotation Z).
5. Optionally set **Mode** to Additive or Direct.
6. Pose the driven items, then click **Key**.
7. Change the driver value, pose again, **Key** again.
8. Move the driver through its range — the driven channels follow the keyed relationship.

### Additive tip

Load driven targets at their rest pose so the captured base is correct. Pose offsets relative to that base, then **Key**. With **Reset to Base after Key** on, the base channels stay free for further animation while the SDK offset supplies the driven pose.

### Live Preview

- **On:** SDK drivers/constraints evaluate (WYSIWYG while scrubbing the driver).
- **Off:** SDK is muted so you can inspect and edit raw transforms.

---

## UI reference

### Main panel (`Driven Key`)

- **Driver** — Load Driver, channel, value, Rest, Capture Rest, Driver to Rest  
- **Driven** — Load / Add / Remove, list, channel toggles, custom paths  
- **Mode** — Additive | Direct  
- **Auto Rest Key**  
- Additive-only: Capture Base, Reset to Base, Auto Reset after Key  
- **Key** / **Delete Key** / **Delete Curves**  
- **Live Preview**

### Key Options (sub-panel)

- Interpolation  
- Extrapolation  

---

## How it works (technical)

- Driven relationships are stored as **object drivers** whose F-curve X axis is the driver property value and Y is the driven output.
- SDK F-curves are identified by a driver variable named `sdk_driver` (type `SINGLE_PROP`, expression type `SUM`).
- **Additive — objects:** drivers on `delta_location`, `delta_rotation_euler`, `delta_scale`.
- **Additive — pose bones:** one Transformation constraint per transform kind; min and max outputs are both driven to the same value so the offset is constant and independent of the constraint source; mix modes ADD (loc/rot) and MULTIPLY (scale).
- **Direct:** drivers on the actual `location` / `rotation_euler` / `rotation_quaternion` / `scale` (or custom) paths.
- Keys closer than `0.01` on the driver axis are treated as the same key and updated in place.

---

## Limitations

- Additive mode does not support quaternion channels (use Direct or Euler).
- Additive rotation on **objects** requires Euler rotation mode.
- Axis-angle rotation is not supported.
- Custom driven paths always use Direct mode.
- A bone cannot drive itself; a path cannot drive itself.

---

## File layout

| File | Role |
|------|------|
| `set_driven_key.py` | Single-file add-on (registration, data, operators, UI) |
| `README.md` | This documentation |

---
