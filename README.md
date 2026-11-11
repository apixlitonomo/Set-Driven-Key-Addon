# Set Driven Key

**Maya-style Set Driven Key for Blender** — drive object or bone channels from any controller, with **Configurations Manager**, **Dynamic Multi-Axis Stacking**, **Additive**, **Direct**, and **Action Constraint** modes, designed for professional character rigging and high viewport performance at scale.

**Addon name (in Blender):** Set Driven Key  
**Version:** 1.5.1
**Minimum Blender:** 3.0.0 (Action Constraint mode also works on 5.0+, which uses slotted/layered Actions)  
**Authors:** OceanMan, CloudKing, Apixelitonomo, U.F.Olive  
**Location:** 3D Viewport → Sidebar (**N**) → **Driven Key**  
**Category:** Animation

---

## Overview

In Maya, **Set Driven Key** links one attribute (the *driver*) to others (the *driven*) so changing the driver value moves the driven channels through authored poses. Blender can do this with drivers and Action constraints, but manual setup is slow, fragile, and error-prone.

This add-on provides an industry-standard interface to:

1. **Manage & Search Configurations:** Save, browse, filter, and load different Set Driven Key setups directly in the UI panel.
2. **Stack Multiple Configurations Dynamically:** Stack different driver axes, controllers, and corrective poses on top of each other without overwriting existing relationships.
3. **Full Data Path Support:** Drive from standard channels, custom properties, or arbitrary data paths (e.g. `bpy.data.objects["Armature"].pose.bones["ClawControl"].scale[0]`).
4. **Three Rigging Modes:** Choose between **Additive (Multi-Axis)**, **Direct**, or **Action Constraint** modes.
5. **Interactive Controls:** Scrub driver values directly from the viewport panel and navigate between keys with **Prev Key** / **Next Key**.
6. **Rigging Helpers:** One-click channel presets, Mesh Shape Key loading, and symmetrical pose mirroring (`.L` <-> `.R`).

---

## Features

### Configuration Manager & Dynamic Stacking

- **Save Configurations:** Save driver settings, driven targets, channel selections, and layer tags into named configurations. Objects are stored as pointers plus name / data-name / library fallbacks; missing items are reported when loading.
- **Export / Import Configs:** Write all configurations to a JSON file and read them back (objects are matched by name).
- **Search & Filter:** Search configurations in real-time within the UI list.
- **Dynamic Layer Stacking:** Each configuration can have its own **Layer Tag**. When set, new relationships create independent stacked layers (using non-destructive Transformation constraints or scoped Action constraints) that compound their offsets cleanly without overwriting earlier configurations.

### Three Rigging Modes

| Mode | Behaviour | Best Used For |
|------|-----------|---------------|
| **Additive (Multi-Axis)** (default) | Non-destructive. Offsets stack on top of base animation. Allows multiple driver axes, bones, and layers to drive the same target channels simultaneously. | Corrective shapes, secondary joint motion, multi-axis corrective setups, and stacking offsets over animator keyframes. |
| **Direct** | Maya-style direct control. The driver takes over the channel and sets its value directly. High-performance evaluation. | Mechanical rigs, foot rolls, finger curls, simple switches, and mesh Shape Keys. |
| **Action Constraint** | Drives an Action Constraint evaluation range (Location/Rotation/Scale drivers use the transform channel; custom properties and other drivers use Evaluation Time; driver value v maps to frame v × 100). Curves store offsets from the captured base (identity at rest), are grouped per bone, and stack with Additive layers on the same channel. After editing keys by hand, press **Refresh Action Range**. Keyframes are stored into a unified Action data-block per driver axis, evaluating multi-bone / multi-channel poses natively. | Complex facial rigs, corrective pose libraries, multi-bone secondary setups, and maximum rig cleanliness. |

### Driver Controls & Full Data Paths

- Load active object or active pose bone in Pose Mode.
- Channels: Location XYZ, Rotation Euler XYZ, Quaternion WXYZ, Scale XYZ, or **Custom Path**.
- Full data path syntax supported:
  - `bpy.data.objects["Armature"].pose.bones["ClawControl"].scale[0]`
  - `data.shape_keys.key_blocks["Smile"].value`
  - Standard relative paths: `["myprop"]`, `location[2]`
- **Interactive Scrubber:** Scrub the driver property directly from the panel without switching views.
- **Key Navigation:** **Prev Key** and **Next Key** buttons jump the driver directly to authored key values.
- **Visual Key Indicator:** Lists existing driver key values for the enabled channels.
- **Rest value helpers:** Capture Driver Rest, Driver to Rest.

### Driven Targets & Channels

- Load **selected objects** or **selected pose bones** in Pose Mode.
- **Channel Presets:** Quick one-click buttons for `All`, `Loc`, `Rot`, `Scale`, and `Clear`.
- **Mesh Shape Keys:** One-click **Add Mesh Shape Keys** button extracts all non-basis shape keys on the active mesh and links them into driven channels.
- Base pose snapshotting for Additive math.

### Rigging & Symmetry Tools

- **Mirror / Flip (`.L` <-> `.R`):** Copy and mirror SDK setups to the counterpart bone in the current mode: Direct curves, Additive constraint layers (`SDK Location/Rotation/Scale [scope]`) or Action Constraint curves (adding the matching Action constraint). Sign flips come from the bones' rest orientation, falling back to Blender's X-mirror convention. Reports how many curves/constraints were mirrored.
- **High Viewport Performance at Scale:**
  - Optimized driver lookup using native collection indexing `animation_data.drivers.find(path, index)` (O(1) lookups instead of O(N) linear scans).
  - Handles rigs with thousands of bones and driven channels with smooth viewport playback (>200 FPS).
- **Auto Rest Key:** Automatically keys the rest state when a curve gets its first key, ensuring channels return to base when the driver returns to rest.
- **Reset to Base after Key:** Automatically restores posed channels back to base after keying in Additive and Action modes.
- **Interpolation & Extrapolation:** Smooth (Bezier), Linear, Stepped (Constant); Constant or Linear extrapolation.

### Editing & Cleanup

- **Delete Key at Driver Value:** Removes keys at the current driver value across enabled driven channels or the active Action.
- **Delete Driven Curves:** Cleans up SDK drivers, unused constraints, and generated Actions while restoring default transforms.
- **Live Preview:** WYSIWYG toggle to enable/mute SDK evaluation (drivers, shape-key drivers, Additive and Action constraints) so you can inspect raw poses.
- **Scrub slider:** a 0–1 slider over an adjustable Min/Max range (set automatically from the driver channel and value; **Auto** button to reset), plus a plain value field.
- **Conflict warnings:** Keying reports channels already controlled by another mode, or by a driver this add-on didn't create, instead of double-driving them.

---

## Installation

1. Download `set_driven_key.py`.
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk** (or Install).
3. Select `set_driven_key.py` and enable **Set Driven Key**.
4. Open the 3D Viewport sidebar (**N**) → **Driven Key**.

**Requirements:** Blender **3.0** or newer (including Blender 4.x).

---

## Quick Start

1. Select your controller object or bone → Click **Load Driver**.
2. Select the driver channel (or enter a custom path, e.g. `bpy.data.objects["Armature"].pose.bones["ClawControl"].scale[0]`).
3. Select the driven bones or objects → Click **Load Driven** (or **Add Mesh Shape Keys**).
4. Choose the channels to drive using the presets (`Loc`, `Rot`, `Scale`, or `All`).
5. Choose your mode (**Additive**, **Direct**, or **Action Constraint**).
6. Pose the driven items and click **Key**.
7. Scrub the driver value (or use the **Scrub Value** slider), adjust the pose, and click **Key** again.
8. Save your setup with **Save Current as Configuration** (+) to edit or stack more configurations on top later.
9. Use **Prev Key** and **Next Key** to review your keyed poses, or scrub the driver to see the animation evaluate in real-time.

---
