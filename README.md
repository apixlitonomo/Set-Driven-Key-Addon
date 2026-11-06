# Set Driven Key

**Maya-style Set Driven Key for Blender**

A Blender add-on that brings Maya’s *Set Driven Key* workflow into Blender. Drive any object or bone channel from a controller attribute, author poses by simply posing and pressing **Key**, and manage complex multi-layer corrective setups with a clean UI.

**Addon name (in Blender):** Set Driven Key  

**Version:** 3.5.0  

**Minimum Blender:** 3.0.0  

**Authors:** OceanMan, CloudKing, Apixelitonomo, U.F.Olive

**Location:** 3D Viewport → Sidebar (**N**) → **Driven Key**  

**Category:** Animation  

---

## Quick Introduction

In Maya, Set Driven Key lets you link one attribute (the *driver*) to one or more other attributes (the *driven*). Moving the driver automatically moves the driven channels through the poses you authored.

Blender can achieve the same result with drivers and Action constraints, but the manual setup is slow, fragile, and easy to break—especially when you need multiple controllers, corrective layers, or shape keys.

This add-on gives you a focused, Maya-like interface that:

- Loads a driver (object / pose bone + channel or any custom data path)
- Loads driven targets (objects, bones, or mesh shape keys)
- Lets you pose and press **Key**
- Supports three evaluation modes (Additive, Direct, Action Constraint)
- Saves named configurations so you can stack multiple independent driver layers
- Provides scrubbing, key navigation, symmetry mirroring, and cleanup tools

Ideal for character rigging: corrective shapes, secondary motion, foot rolls, finger curls, facial systems, and multi-axis driven setups—while tring to stay performant even on large rigs.

---
**Known issues**

**Action Constraint Mode – Blender 5.0+ Compatibility**

  **Action Constraint mode currently does not work in Blender 5.0 and newer.**
#### Reason
Blender 5.0 removed the legacy Action API (`action.fcurves`, `action.groups`, `action.id_root`).

Actions now use the new **Slotted Actions** system (Slots → Layers → Strips → Channelbags).

The current implementation still relies on the old `action.fcurves` access when creating and keying the `SDK_Action_*` data-blocks used by Action Constraints. This raises:

  AttributeError: 'Action' object has no attribute 'fcurves'

#### Status
- **Direct mode** → works
- **Additive mode** → works
- **Action Constraint mode** → broken on Blender 5.0+

#### Workaround
Use **Direct** or **Additive** mode instead.  
They produce the same (or better) results for most Set Driven Key use cases and avoid the Action system entirely.

A future update will adapt the Action Constraint mode to the new slotted Action API.

---

## How It Works Internally

The add-on never invents a new evaluation system. It builds standard Blender animation data (drivers, Transformation constraints, or Action constraints) in a structured, reversible way and manages them through a single scene settings block (`scene.sdk_settings`).

### Core Data Model

- **`SDK_Settings`** (scene property) holds the current driver, driven list, channel toggles, mode, interpolation, rest value, layer tag, and a collection of saved configurations.
- **`SDK_DrivenItem`** stores a pointer to an object (and optional bone name) plus a snapshot of its base/rest transform (location, Euler, quaternion, scale). This snapshot is the reference for Additive offset math.
- **`SDK_ConfigItem`** serializes a complete setup (driver, channels, mode, layer tag, driven targets) so it can be reloaded or stacked later.

Automatic Driver resolution supports:

- Standard channels (`location:0`, `rotation_euler:1`, `scale:2`, quaternion components, …)
- Relative custom paths (`["myprop"]`, `location[2]`)
- Full absolute paths (`bpy.data.objects["Armature"].pose.bones["Claw"].scale[0]`)
- Shape-key paths (`data.shape_keys.key_blocks["Smile"].value`)

A scope key is derived from the driver identity + optional **Layer Tag**. This key is embedded in constraint/action names so multiple configurations can coexist without overwriting each other.

### The Three Modes

#### 1. Direct Mode
Classic Maya-style takeover.

- A normal Blender driver is added directly on the driven channel.
- The driver variable is named `sdk_driver` and points at the controller’s data path.
- Keyframes are written on the driver f-curve: X = driver value, Y = driven value.
- When the driver changes, Blender evaluates the curve and sets the channel.


#### 2. Additive (Multi-Axis) Mode — default
Non-destructive stacking.

- A **Transformation** constraint is created on the pose bone / object.
- The constraint is named `SDK Location/Rotation/Scale [scope_key]` so each driver axis/layer gets its own independent constraint.
- The constraint uses a hidden dummy Empty (`SDK_Constraint_Target`) as target (required by Blender’s constraint system; the Empty itself is never evaluated meaningfully).
- Drivers are placed on the constraint’s `to_min_*` / `to_max_*` properties.
- On keying, the add-on computes the *offset* from the captured rest pose and writes that offset into the constraint min/max curves.
- Because each layer uses its own constraint and `mix_mode = ADD` (or `MULTIPLY` for scale), multiple drivers, axes, and configurations can push the same bone simultaneously without fighting.

This is the mode used for corrective shapes and secondary motion that must stack on top of animator keys or other SDK layers.

#### 3. Action Constraint Mode
Pose-library style evaluation.

- A dedicated Action data-block is created per driver scope (`SDK_Action_<scope_key>`).
- An **Action** constraint is added to each driven bone/object.
- Driver value is mapped to the constraint’s evaluation range (internally scaled by 100 so frames stay integer-friendly).
- Keyframes are written into the Action at `frame = driver_value * 100`.
- For non-transform custom drivers, a proxy driver is placed on the dummy Empty’s Location X; the Action constraint then reads that proxy.

This keeps the rig clean (one Action per driver axis) and is good enough for multi-bone facial poses or corrective libraries.

### Supporting Systems

- **Path helpers** (`set_by_path`, `resolve_driver_target`, `full_path`, …) robustly handle array indices, ID properties, shape keys, and bone-relative paths.
- **O(1) driver lookup** via `animation_data.drivers.find(path, index)` (with a linear fallback) keeps UI and evaluation fast on large rigs.
- **Auto Rest Key** inserts a key at the driver’s rest value the first time a curve is keyed, guaranteeing a return to base.
- **Live Preview** mutes/unmutes all SDK drivers and constraints so you can inspect the raw pose.
- **Mirror operator** duplicates curves and constraints across `.L` ↔ `.R` pairs with proper axis flipping, working in all three modes.
- **Dummy target garbage collection** removes the helper Empty when no constraints reference it (on curve deletion or add-on unregister).
- **Keyed-value cache** avoids expensive scans on every UI redraw.

All SDK-created drivers are identifiable by the variable name `sdk_driver`. Cleanup operators only touch those curves and the correspondingly named constraints/actions, restoring default transforms where possible.

---

## Features

### Configuration Manager & Dynamic Stacking
- Save / load named configurations (driver, targets, channels, mode, layer tag).
- Real-time search/filter in the UI list.
- **Layer Tag** creates independent stacked constraints/actions so multiple setups compound cleanly.

### Driver & Driven Controls
- Load active object or pose bone.
- Full data-path support (relative, absolute, shape keys, custom properties).
- Interactive scrubber + Prev/Next Key navigation.
- Visual list of existing key values.
- One-click channel presets (All / Loc / Rot / Scale / Clear).
- One-click **Add Mesh Shape Keys**.

### Rigging Helpers
- Symmetry mirror (`.L` ↔ `.R`) across all modes.
- Auto Rest Key and optional Reset-to-Base after keying.
- Interpolation (Bezier / Linear / Constant) and extrapolation controls.
- Delete key at current driver value or delete entire driven curves + constraints.

### Performance
- Optimized driver lookups.
- Designed for rigs with thousands of bones/channels while maintaining high viewport FPS. (hopefully)

---

## Installation

1. Download `set_driven_key.py`.
2. In Blender: **Edit → Preferences → Add-ons → Install…** (or Install from Disk).
3. Select the file and enable **Set Driven Key**.
4. Open the 3D Viewport sidebar (**N**) → tab **Driven Key**.

**Requirements:** Blender 3.0 or newer (including 4.x).

---

## Quick Start

1. Select the controller object or bone → **Load Driver**.
2. Choose the driver channel (or enter a custom path).
3. Select the driven objects/bones → **Load Driven** (or **Add Mesh Shape Keys**).
4. Enable the channels you want to drive (use the presets).
5. Choose mode: **Additive**, **Direct**, or **Action Constraint**.
6. Pose the driven items and click **Key**.
7. Scrub the driver (or use the Scrub slider), re-pose, and **Key** again.
8. Optionally save the setup with **Save Current as Configuration**.
9. Use **Prev Key** / **Next Key** or the scrubber to review the result in real time.

---

## License / Credits

Authors: OceanMan, CloudKing, Apixelitonomo, U.F.Olive

Feel free to open issues or pull requests on the repository.
