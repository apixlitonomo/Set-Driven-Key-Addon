# Set Driven Key

**Maya-style Set Driven Key for Blender** — drive object or bone channels from a controller, with **Additive**, **Direct**, and **Action Constraint** modes, designed for professional character rigging and high viewport performance at scale.

**Addon name (in Blender):** Set Driven Key  
**Version:** 1.2.0
**Minimum Blender:** 3.0.0  
**Authors:** Oceanman, CloudKing  
**Location:** 3D Viewport → Sidebar (**N**) → **Driven Key**  
**Category:** Animation

---

## Overview

In Maya, **Set Driven Key** links one attribute (the *driver*) to others (the *driven*) so changing the driver value moves the driven channels through authored poses. Blender can do this with drivers and Action constraints, but manual setup is slow, fragile, and error-prone.

This add-on provides an industry-standard interface to:

1. Load a **driver** (object or pose bone + channel) with an interactive **Scrub Value** slider.
2. Load **driven** targets (objects, bones, and mesh shape keys).
3. Select channels with one-click **Channel Presets** (All, Loc, Rot, Scale, Clear).
4. Choose between **Additive**, **Direct**, or **Action Constraint** modes.
5. Author poses and press **Key**.
6. Jump between authored keys with **Prev Key** / **Next Key** navigation.
7. Mirror poses across symmetrical skeletons (`.L` <-> `.R`).

---

## Features

### Three Rigging Modes

| Mode | Behaviour | Best Used For |
|------|-----------|---------------|
| **Additive** (default) | Non-destructive. Offsets stack on top of base animation. You can still keyframe, pose, and animate the same channels. Objects use delta transforms; bones use Transformation constraints. | Corrective shapes, secondary joint motion, and stacking offsets over animator keyframes. |
| **Direct** | Maya-style direct control. The driver takes over the channel and sets its value directly. High-performance evaluation. | Mechanical rigs, foot rolls, finger curls, simple switches, and mesh Shape Keys. |
| **Action Constraint** | Drives an Action Constraint evaluation range. Keyframes are stored into a unified Action data-block per driver, evaluating multi-bone / multi-channel poses natively. | Complex facial rigs, corrective pose libraries, multi-bone secondary setups, and maximum rig cleanliness. |

### Driver Controls

- Load the **active object**, or the **active pose bone** in Pose Mode.
- Channels: Location XYZ, Rotation Euler XYZ, Quaternion WXYZ, Scale XYZ, or a **custom data path**.
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

- **Mirror / Flip (`.L` <-> `.R`):** Copy and mirror SDK drivers and curves from left to right (or right to left) with proper axis flipping across symmetrical bones.
- **High Viewport Performance at Scale:**
  - Optimized driver lookup using native collection indexing `animation_data.drivers.find(path, index)` (O(1) lookups instead of O(N) linear scans).
  - Handles rigs with thousands of bones and driven channels with smooth viewport playback (>200 FPS).
- **Auto Rest Key:** Automatically keys the rest state when a curve gets its first key, ensuring channels return to base when the driver returns to rest.
- **Reset to Base after Key:** Automatically restores posed channels back to base after keying in Additive and Action modes.
- **Interpolation & Extrapolation:** Smooth (Bezier), Linear, Stepped (Constant); Constant or Linear extrapolation.

### Editing & Cleanup

- **Delete Key at Driver Value:** Removes keys at the current driver value across enabled driven channels or the active Action.
- **Delete Driven Curves:** Cleans up SDK drivers, unused constraints, and generated Actions while restoring default transforms.
- **Live Preview:** WYSIWYG toggle to enable/mute SDK evaluation so you can inspect raw poses.

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
2. Select the driver channel (e.g., `Location Y` or a custom property).
3. Select the driven bones or objects → Click **Load Driven** (or **Add Mesh Shape Keys**).
4. Choose the channels to drive using the presets (`Loc`, `Rot`, `Scale`, or `All`).
5. Choose your mode (**Additive**, **Direct**, or **Action Constraint**).
6. Pose the driven items and click **Key**.
7. Scrub the driver value (or use the **Scrub Value** slider), adjust the pose, and click **Key** again.
8. Use **Prev Key** and **Next Key** to review your keyed poses, or scrub the driver to see the animation evaluate in real-time.

---
