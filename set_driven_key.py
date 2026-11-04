bl_info = {
    "name": "Set Driven Key",
    "author": "OceanMan, CloudKing",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Driven Key",
    "description": "Professional Set Driven Key for Blender with Additive (Delta/Transform), Direct, and Action Constraint modes",
    "category": "Animation",
}

import math
import re
import traceback

import bpy
from bpy.props import (
    BoolProperty,
    BoolVectorProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Euler, Quaternion

# Name of the driver variable the add-on creates. Used to identify SDK-managed curves.
SDK_VAR = "sdk_driver"

# Additive mode on pose bones uses one Transformation constraint per transform kind.
SDK_CON = {"loc": "SDK Location", "rot": "SDK Rotation", "scale": "SDK Scale"}
CON_SUFFIX = {"loc": "", "rot": "_rot", "scale": "_scale"}
DELTA_ATTR = {"loc": "delta_location", "rot": "delta_rotation_euler", "scale": "delta_scale"}
DIRECT_ATTR = {"loc": "location", "rot": "rotation_euler", "scale": "scale"}

# Action constraint naming
SDK_ACTION_CON = "SDK Action"
SDK_ACTION_PREFIX = "SDK_Action_"

KEY_EPS = 0.01  # Blender treats keys closer than this (on the X axis) as the same key

DUMMY_TARGET_NAME = "SDK_Constraint_Target"


# --------------------------------------------------------------------------
# Path / value helpers
# --------------------------------------------------------------------------

_CUSTOM_RE = re.compile(r"^(.+)\[(\d+)\]$")


def parse_custom(text):
    """'location[1]' -> ('location', 1);  '["prop"]' -> ('["prop"]', -1)"""
    text = text.strip()
    m = _CUSTOM_RE.match(text)
    if m:
        return m.group(1), int(m.group(2))
    return text, -1


def full_path(bone, rel):
    """Data path relative to the Object, optionally going through a pose bone."""
    if not bone:
        return rel
    base = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone)
    return base + (rel if rel.startswith("[") else "." + rel)


def var_path(path, index):
    return "%s[%d]" % (path, index) if index >= 0 else path


def resolve_id_and_path(obj, path):
    """Determine the host ID block and relative path for driving.

    Supports Mesh Shape Keys (which live on Key ID blocks) and standard Object data.
    """
    if path.startswith("data.shape_keys."):
        if obj and hasattr(obj, "data") and hasattr(obj.data, "shape_keys") and obj.data.shape_keys:
            return obj.data.shape_keys, path[len("data.shape_keys."):]
    return obj, path


def get_value(obj, path, index):
    id_block, rel_path = resolve_id_and_path(obj, path)
    value = id_block.path_resolve(rel_path)
    if index >= 0:
        value = value[index]
    return float(value)


# Matches a data path that ends in a custom (ID) property: <parent>["name"] or <parent>["name"][2]
_IDPROP_RE = re.compile(
    r'^(?P<parent>.*?)\[(?P<q>["\'])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)\]'
    r'(?:\[(?P<idx>\d+)\])?$'
)


def set_by_path(obj, path, index, value):
    """Assign `value` to the property at `path` (optionally an array element).

    Handles RNA attributes, Shape Keys, and custom/ID properties.
    """
    id_block, rel_path = resolve_id_and_path(obj, path)
    m = _IDPROP_RE.match(rel_path)
    if m:
        parent = m.group("parent")
        owner = id_block.path_resolve(parent) if parent else id_block
        name = bpy.utils.unescape_identifier(m.group("name"))
        idx = int(m.group("idx")) if m.group("idx") is not None else index
        if idx >= 0:
            owner[name][idx] = value
        else:
            owner[name] = value
        return

    if "." in rel_path:
        parent, prop = rel_path.rsplit(".", 1)
        owner = id_block.path_resolve(parent)
    else:
        owner, prop = id_block, rel_path
    if index >= 0:
        getattr(owner, prop)[index] = value
    else:
        setattr(owner, prop, value)


def identity_for(path):
    return 1.0 if path.endswith("_scale") or path.endswith("scale") else 0.0


def owner_of(item):
    """The Object or PoseBone whose base channels this item represents."""
    obj = item.obj
    if obj is None:
        return None
    if item.bone:
        if obj.pose is None:
            return None
        return obj.pose.bones.get(item.bone)
    return obj


def _build_driver_items():
    items = []
    for attr, label, comps in (
        ("location", "Location", "XYZ"),
        ("rotation_euler", "Rotation", "XYZ"),
        ("rotation_quaternion", "Quaternion", "WXYZ"),
        ("scale", "Scale", "XYZ"),
    ):
        for i, c in enumerate(comps):
            items.append(("%s:%d" % (attr, i), "%s %s" % (label, c), ""))
    items.append((
        "CUSTOM", "Custom Path...",
        'Any data path, e.g. ["myprop"], location[2] or modifiers["Bevel"].width',
    ))
    return items


DRIVER_ITEMS = _build_driver_items()


def driver_spec(s):
    if s.driver_channel == "CUSTOM":
        return parse_custom(s.driver_custom)
    attr, idx = s.driver_channel.split(":")
    return attr, int(idx)


def transform_specs(s):
    """[(kind, axis)] for the Location / Rotation / Scale toggles."""
    out = []
    for kind, vec in (("loc", s.ch_loc), ("rot", s.ch_rot), ("scale", s.ch_scale)):
        for i, on in enumerate(vec):
            if on:
                out.append((kind, i))
    return out


def quat_specs(s):
    return [i for i, on in enumerate(s.ch_quat) if on]


def custom_specs(s):
    return [parse_custom(t) for t in s.ch_custom.split(";") if t.strip()]


def capture_driver_rest(s):
    """Remember the driver's current value as its rest value."""
    if s.driver_obj is None:
        return
    try:
        rel, idx = driver_spec(s)
        s.driver_rest = get_value(s.driver_obj, full_path(s.driver_bone, rel), idx)
    except Exception:  # noqa: BLE001
        pass


def get_or_create_dummy_target():
    """Ensure a dummy non-evaluating Empty object exists to satisfy Transformation constraints."""
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        empty = bpy.data.objects.new(DUMMY_TARGET_NAME, None)
        empty.empty_display_size = 0.001
        empty.hide_render = True
        empty.hide_viewport = True
        scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
        if scene and scene.collection:
            scene.collection.objects.link(empty)
    return empty


# --------------------------------------------------------------------------
# Driver / curve helpers (High performance O(1) lookups)
# --------------------------------------------------------------------------

def find_driver_fcurve(target_id, path, index):
    """Fast driver lookup using drivers.find(path, index)."""
    ad = target_id.animation_data
    if ad is None:
        return None
    idx = max(index, 0)
    try:
        return ad.drivers.find(path, index=idx)
    except Exception:  # Fallback for older API or custom property paths without index
        for fc in ad.drivers:
            if fc.data_path == path and (index < 0 or fc.array_index == idx):
                return fc
    return None


def is_sdk_fcurve(fc):
    return any(v.name == SDK_VAR for v in fc.driver.variables)


def ensure_driver(obj, path, index, drv_obj, drv_data_path, mute):
    """Return (fcurve, message). fcurve is None if the channel can't be used."""
    target_id, rel_path = resolve_id_and_path(obj, path)
    fc = find_driver_fcurve(target_id, rel_path, index)
    if fc is None:
        try:
            fc = target_id.driver_add(rel_path, index) if index >= 0 else target_id.driver_add(rel_path)
        except Exception as e:  # noqa: BLE001
            return None, "Cannot drive '%s': %s" % (rel_path, e)
        if isinstance(fc, (list, tuple)):
            target_id.driver_remove(rel_path)
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        fc.mute = mute
    elif not is_sdk_fcurve(fc):
        return None, "'%s' already has a driver that wasn't made by this add-on" % rel_path

    drv = fc.driver
    var = next((v for v in drv.variables if v.name == SDK_VAR), None)
    drv_id_type = getattr(drv_obj, "rna_type", None)
    id_type_name = "OBJECT"
    if drv_id_type:
        type_str = drv_id_type.name.upper()
        if type_str in ("KEY", "OBJECT", "ARMATURE", "SCENE", "MATERIAL", "NODETREE"):
            id_type_name = type_str

    same = (
        var is not None
        and var.targets[0].id == drv_obj
        and var.targets[0].data_path == drv_data_path
    )
    if not same:
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[0], fast=True)
        for v in list(drv.variables):
            drv.variables.remove(v)
        var = drv.variables.new()
        var.name = SDK_VAR
        var.type = "SINGLE_PROP"
        t = var.targets[0]
        t.id_type = id_type_name
        t.id = drv_obj
        t.data_path = drv_data_path
        drv.type = "SUM"

    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    return fc, ""


def key_curve(fc, x, y, interpolation, extrapolation):
    """Set the key at driver value `x` to `y` (replacing a key already there)."""
    kps = fc.keyframe_points
    kp = next((k for k in kps if abs(k.co.x - x) < KEY_EPS), None)
    if kp is None:
        kps.add(1)
        kp = kps[len(kps) - 1]
        kp.co = (x, y)
        kp.handle_left = (x - 1.0, y)
        kp.handle_right = (x + 1.0, y)
        kp.handle_left_type = "AUTO_CLAMPED"
        kp.handle_right_type = "AUTO_CLAMPED"
    else:
        dy = y - kp.co[1]
        kp.co[1] = y
        kp.handle_left[1] += dy
        kp.handle_right[1] += dy
    kp.interpolation = interpolation
    fc.extrapolation = extrapolation
    fc.update()


def add_rest_key(s, fc, x, rest_y):
    """On a brand-new curve, also key the rest state (driver at its rest value)."""
    if rest_y is None or not s.auto_rest_key:
        return
    if len(fc.keyframe_points) == 0 and abs(x - s.driver_rest) >= KEY_EPS:
        key_curve(fc, s.driver_rest, rest_y, s.interpolation, s.extrapolation)


def safe_call(fn, label, *args):
    """Run one key operation. A failure becomes a message instead of aborting everything."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return "%s failed: %s" % (label, e)


# --------------------------------------------------------------------------
# Additive mode helpers
# --------------------------------------------------------------------------

def additive_targets(item, kind, axis):
    """(path, index) pairs the SDK drives for one transform channel in additive mode."""
    if not item.bone:
        return [(DELTA_ATTR[kind], axis)]
    a = "xyz"[axis]
    rel = 'constraints["%s"].' % SDK_CON[kind]
    return [
        (full_path(item.bone, rel + "to_%s_%s%s" % (m, a, CON_SUFFIX[kind])), -1)
        for m in ("min", "max")
    ]


def ensure_constraint(item, kind, mute):
    """Ensure a Transformation constraint exists on the pose bone."""
    pb = owner_of(item)
    name = SDK_CON[kind]
    con = pb.constraints.get(name)
    if con is None:
        con = pb.constraints.new("TRANSFORM")
        con.name = name
        con.owner_space = "LOCAL"
        con.target_space = "LOCAL"
        con.map_from = "LOCATION"
        con.use_motion_extrapolate = True
        for a in "xyz":
            setattr(con, "from_min_" + a, 0.0)
            setattr(con, "from_max_" + a, 1.0)
        con.map_to_x_from = "X"
        con.map_to_y_from = "Y"
        con.map_to_z_from = "Z"
        ident = 1.0 if kind == "scale" else 0.0
        for a in "xyz":
            setattr(con, "to_min_%s%s" % (a, CON_SUFFIX[kind]), ident)
            setattr(con, "to_max_%s%s" % (a, CON_SUFFIX[kind]), ident)
        if kind == "loc":
            con.map_to = "LOCATION"
            con.mix_mode = "ADD"
        elif kind == "rot":
            con.map_to = "ROTATION"
            con.mix_mode_rot = "ADD"
        else:
            con.map_to = "SCALE"
            con.mix_mode_scale = "MULTIPLY"
        con.mute = mute
    con.target = get_or_create_dummy_target()
    con.subtarget = ""
    return con


def capture(item):
    """Snapshot the base channels (the 'rest' that additive offsets are measured from)."""
    t = owner_of(item)
    if t is None:
        return
    item.rest_loc = t.location
    item.rest_euler = t.rotation_euler
    item.rest_quat = t.rotation_quaternion
    item.rest_scale = t.scale


def current_euler(owner):
    if owner.rotation_mode == "QUATERNION":
        return owner.rotation_quaternion.to_euler()
    return owner.rotation_euler.copy()


def rest_euler(item, owner):
    if owner.rotation_mode == "QUATERNION":
        return Quaternion(item.rest_quat).to_euler()
    return Euler(item.rest_euler)


def reset_base(item, specs):
    owner = owner_of(item)
    if owner is None:
        return
    for kind, axis in specs:
        if kind == "loc":
            owner.location[axis] = item.rest_loc[axis]
        elif kind == "scale":
            owner.scale[axis] = item.rest_scale[axis]
        else:
            if owner.rotation_mode == "QUATERNION":
                owner.rotation_quaternion = item.rest_quat
            else:
                owner.rotation_euler[axis] = item.rest_euler[axis]


def key_additive(s, item, kind, axis, drv_obj, d_path, d_idx, d_var_path, x):
    """Key one transform channel as an offset from the captured base. Returns '' or a message."""
    obj = item.obj
    owner = owner_of(item)
    if item.bone and obj == drv_obj and item.bone == s.driver_bone:
        return "Bone '%s' can't drive itself" % item.bone

    if kind == "loc":
        base_off = owner.location[axis] - item.rest_loc[axis]
    elif kind == "rot":
        mode = owner.rotation_mode
        if mode == "AXIS_ANGLE":
            return "Axis-angle rotation isn't supported"
        if not item.bone and mode == "QUATERNION":
            return "Additive rotation on objects needs Euler rotation mode"
        base_off = current_euler(owner)[axis] - rest_euler(item, owner)[axis]
        if mode == "QUATERNION":
            base_off = (base_off + math.pi) % (2.0 * math.pi) - math.pi
    else:
        rest = item.rest_scale[axis]
        if abs(rest) < 1e-8:
            return "Base scale is zero on %s, can't compute a ratio" % obj.name
        base_off = owner.scale[axis] / rest

    targets = additive_targets(item, kind, axis)
    for path, idx in targets:
        if obj == drv_obj and path == d_path and max(idx, 0) == max(d_idx, 0):
            return "'%s' can't drive itself" % path

    ident = 1.0 if kind == "scale" else 0.0
    existing = ident
    if s.live_preview:
        p0, i0 = targets[0]
        if find_driver_fcurve(obj, p0, i0) is not None:
            try:
                existing = get_value(obj, p0, i0)
            except Exception:  # noqa: BLE001
                existing = ident
    value = existing * base_off if kind == "scale" else existing + base_off

    if item.bone:
        try:
            ensure_constraint(item, kind, not s.live_preview)
        except Exception as e:  # noqa: BLE001
            return "Can't set up constraint on '%s': %s" % (item.bone, e)

    for path, idx in targets:
        mute = False if item.bone else not s.live_preview
        fc, msg = ensure_driver(obj, path, idx, drv_obj, d_var_path, mute)
        if fc is None:
            return msg
        add_rest_key(s, fc, x, ident)
        key_curve(fc, x, value, s.interpolation, s.extrapolation)
    return ""


def key_direct(s, item, attr, idx, drv_obj, d_path, d_idx, d_var_path, x):
    """Maya-style direct channel drive. Returns '' or a message."""
    obj = item.obj
    path = full_path(item.bone, attr)
    if obj == drv_obj and path == d_path and max(idx, 0) == max(d_idx, 0):
        return "'%s' can't drive itself" % path
    try:
        y = get_value(obj, path, idx)
    except Exception:  # noqa: BLE001
        return "Can't read '%s'" % var_path(path, idx)
    fc, msg = ensure_driver(obj, path, idx, drv_obj, d_var_path, not s.live_preview)
    if fc is None:
        return msg

    rest_y = None
    if idx >= 0:
        if attr == "location":
            rest_y = item.rest_loc[idx]
        elif attr == "rotation_euler":
            rest_y = item.rest_euler[idx]
        elif attr == "rotation_quaternion":
            rest_y = item.rest_quat[idx]
        elif attr == "scale":
            rest_y = item.rest_scale[idx]
    add_rest_key(s, fc, x, rest_y)
    key_curve(fc, x, y, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Action Constraint Mode Helpers
# --------------------------------------------------------------------------

def action_transform_channel(s):
    """Map driver channel to Action Constraint transform_channel enum identifier."""
    if s.driver_channel.startswith("location:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")[idx]
    if s.driver_channel.startswith("rotation_euler:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("ROTATION_X", "ROTATION_Y", "ROTATION_Z")[idx]
    if s.driver_channel.startswith("scale:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("SCALE_X", "SCALE_Y", "SCALE_Z")[idx]
    return "LOCATION_X"


def get_action_for_driver(s):
    """Return or create a dedicated Action for the current driver in Action Constraint mode."""
    driver_name = s.driver_bone if s.driver_bone else (s.driver_obj.name if s.driver_obj else "Driver")
    chan = s.driver_channel.replace(":", "_")
    act_name = "%s%s_%s" % (SDK_ACTION_PREFIX, driver_name, chan)
    act = bpy.data.actions.get(act_name)
    if act is None:
        act = bpy.data.actions.new(act_name)
        if hasattr(act, "id_root"):
            try:
                act.id_root = "OBJECT"
            except Exception:  # noqa: BLE001
                pass
        act.use_fake_user = True
    return act


def ensure_action_constraint(s, item, action):
    """Ensure an Action constraint exists on the target bone or object."""
    owner = owner_of(item)
    if owner is None:
        return None, "Target owner not found"
    con = owner.constraints.get(SDK_ACTION_CON)
    if con is None:
        con = owner.constraints.new("ACTION")
        con.name = SDK_ACTION_CON
        con.target_space = "LOCAL"
        if hasattr(con, "mix_mode"):
            try:
                con.mix_mode = "BEFORE_SPLIT"
            except Exception:  # noqa: BLE001
                pass
        if hasattr(con, "show_expanded"):
            con.show_expanded = False
    con.target = s.driver_obj
    con.subtarget = s.driver_bone or ""
    con.action = action
    con.transform_channel = action_transform_channel(s)
    con.mute = not s.live_preview
    return con, ""


def update_action_constraint_ranges(action, s):
    """Update min/max and frame_start/end across all constraints using this action."""
    if not action or not action.fcurves:
        return
    points = [kp.co.x for fc in action.fcurves for kp in fc.keyframe_points]
    if not points:
        return
    min_frame = min(points)
    max_frame = max(points)
    scale = 100.0
    val_min = min_frame / scale
    val_max = max_frame / scale
    if abs(val_max - val_min) < 1e-5:
        val_max = val_min + 1.0
        max_frame = min_frame + 100.0

    seen_owners = set()
    for item in s.driven_items:
        owner = owner_of(item)
        if owner is None or owner.as_pointer() in seen_owners:
            continue
        seen_owners.add(owner.as_pointer())
        con = owner.constraints.get(SDK_ACTION_CON)
        if con and con.action == action:
            con.frame_start = int(round(min_frame))
            con.frame_end = int(round(max_frame))
            con.min = val_min
            con.max = val_max


def key_action_mode(s, item, attr, idx, action, x):
    """Key one channel into the shared Action for Action Constraint mode."""
    obj = item.obj
    if item.bone and obj == s.driver_obj and item.bone == s.driver_bone:
        return "Bone '%s' can't drive itself" % item.bone
    path = full_path(item.bone, attr)
    try:
        y = get_value(obj, path, idx)
    except Exception:  # noqa: BLE001
        return "Can't read '%s'" % var_path(path, idx)

    con, msg = ensure_action_constraint(s, item, action)
    if con is None:
        return msg

    frame = x * 100.0
    fc = action.fcurves.find(path, index=max(idx, 0))
    if fc is None:
        fc = action.fcurves.new(path, index=max(idx, 0))

    if s.auto_rest_key and len(fc.keyframe_points) == 0 and abs(x - s.driver_rest) >= KEY_EPS:
        rest_frame = s.driver_rest * 100.0
        rest_y = None
        if idx >= 0:
            if attr == "location":
                rest_y = item.rest_loc[idx]
            elif attr == "rotation_euler":
                rest_y = item.rest_euler[idx]
            elif attr == "rotation_quaternion":
                rest_y = item.rest_quat[idx]
            elif attr == "scale":
                rest_y = item.rest_scale[idx]
        if rest_y is not None:
            key_curve(fc, rest_frame, rest_y, s.interpolation, s.extrapolation)

    key_curve(fc, frame, y, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------

def all_targets(s, item):
    """Every (path, index) the enabled channels could be driven through."""
    out = []
    for kind, axis in transform_specs(s):
        out.append((full_path(item.bone, DIRECT_ATTR[kind]), axis))
        out.extend(additive_targets(item, kind, axis))
    for i in quat_specs(s):
        out.append((full_path(item.bone, "rotation_quaternion"), i))
    for rel, idx in custom_specs(s):
        out.append((full_path(item.bone, rel), idx))
    return out


def get_all_keyed_values(s):
    """Return sorted unique list of keyed driver values for currently enabled driven channels."""
    if s.driver_obj is None:
        return []
    values = set()
    if s.mode == "ACTION":
        driver_name = s.driver_bone if s.driver_bone else s.driver_obj.name
        chan = s.driver_channel.replace(":", "_")
        act = bpy.data.actions.get("%s%s_%s" % (SDK_ACTION_PREFIX, driver_name, chan))
        if act:
            for fc in act.fcurves:
                for kp in fc.keyframe_points:
                    values.add(round(kp.co.x / 100.0, 4))
        return sorted(values)

    for item in s.driven_items:
        obj = item.obj
        if obj is None:
            continue
        for path, idx in all_targets(s, item):
            target_id, rel_path = resolve_id_and_path(obj, path)
            fc = find_driver_fcurve(target_id, rel_path, idx)
            if fc and is_sdk_fcurve(fc):
                for kp in fc.keyframe_points:
                    values.add(round(kp.co.x, 4))
    return sorted(values)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _preview_update(self, context):
    """Toggle SDK effect on/off so animators can inspect raw base values."""
    on = self.live_preview
    seen = set()
    for item in self.driven_items:
        obj = item.obj
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        ad = obj.animation_data
        if ad:
            for fc in ad.drivers:
                if not is_sdk_fcurve(fc) or ".constraints[" in fc.data_path:
                    continue
                fc.mute = not on
                if not on and fc.data_path.startswith("delta_"):
                    try:
                        set_by_path(obj, fc.data_path, fc.array_index, identity_for(fc.data_path))
                    except Exception:  # noqa: BLE001
                        pass
        if hasattr(obj, "data") and hasattr(obj.data, "shape_keys") and obj.data.shape_keys:
            sk_ad = obj.data.shape_keys.animation_data
            if sk_ad:
                for fc in sk_ad.drivers:
                    if is_sdk_fcurve(fc):
                        fc.mute = not on
        if obj.pose:
            for pb in obj.pose.bones:
                for con in pb.constraints:
                    if con.name in SDK_CON.values() or con.name == SDK_ACTION_CON:
                        con.mute = not on
        for con in obj.constraints:
            if con.name == SDK_ACTION_CON:
                con.mute = not on


def _driver_changed(self, context):
    capture_driver_rest(self)


def _driver_current_get(self):
    if self.driver_obj is None:
        return 0.0
    try:
        rel, idx = driver_spec(self)
        return get_value(self.driver_obj, full_path(self.driver_bone, rel), idx)
    except Exception:  # noqa: BLE001
        return 0.0


def _driver_current_set(self, value):
    if self.driver_obj is None:
        return
    try:
        rel, idx = driver_spec(self)
        set_by_path(self.driver_obj, full_path(self.driver_bone, rel), idx, value)
        if bpy.context:
            bpy.context.view_layer.update()
    except Exception:  # noqa: BLE001
        pass


class SDK_DrivenItem(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)
    bone: StringProperty()
    rest_loc: FloatVectorProperty(size=3)
    rest_euler: FloatVectorProperty(size=3)
    rest_quat: FloatVectorProperty(size=4, default=(1.0, 0.0, 0.0, 0.0))
    rest_scale: FloatVectorProperty(size=3, default=(1.0, 1.0, 1.0))


class SDK_Settings(PropertyGroup):
    # Driver
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_bone: StringProperty()
    driver_channel: EnumProperty(
        name="Channel", items=DRIVER_ITEMS, default="location:0",
        description="Driver channel", update=_driver_changed,
    )
    driver_custom: StringProperty(
        name="Path", description='Data path, e.g. ["myprop"], location[2] or modifiers["Bevel"].width',
        update=_driver_changed,
    )
    driver_rest: FloatProperty(
        name="Rest",
        description="Driver value at which the driven channels are in their original state. "
                    "Captured on load or channel change; editable",
    )
    driver_scrub: FloatProperty(
        name="Scrub Driver",
        description="Scrub current driver property value directly from the UI",
        get=_driver_current_get,
        set=_driver_current_set,
    )
    auto_rest_key: BoolProperty(
        name="Auto Rest Key",
        description="When a driven curve gets its first key, also key the rest state at the "
                    "driver's rest value, so driven channels return to base when driver does",
        default=True,
    )

    # Driven
    driven_items: CollectionProperty(type=SDK_DrivenItem)
    driven_index: IntProperty()
    ch_loc: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_rot: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_quat: BoolVectorProperty(size=4, default=(False,) * 4)
    ch_scale: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_custom: StringProperty(
        name="Custom",
        description='Extra driven data paths separated by ";", e.g. ["myprop"];data.shape_keys.key_blocks["Key 1"].value. '
                    "Custom paths use Direct mode",
    )

    # Behaviour
    mode: EnumProperty(
        name="Mode",
        items=[
            ("ADDITIVE", "Additive",
             "Non-destructive: driven offsets stack on top of base animation "
             "(delta transforms on objects, Transformation constraints on bones)"),
            ("DIRECT", "Direct",
             "Maya-style: driver takes over channels directly"),
            ("ACTION", "Action Constraint",
             "Professional rigging: driver drives an Action Constraint eval range. "
             "Great for complex multi-bone pose sets, facial shapes, and high viewport performance"),
        ],
        default="ADDITIVE",
    )
    auto_reset: BoolProperty(
        name="Reset to Base after Key",
        description="After keying, put posed channels back to the captured base (Additive mode)",
        default=True,
    )
    interpolation: EnumProperty(
        name="Interpolation",
        items=[
            ("BEZIER", "Smooth", "Smooth Bezier curve between keys"),
            ("LINEAR", "Linear", "Straight lines between keys"),
            ("CONSTANT", "Stepped", "Hold value until next key"),
        ],
        default="BEZIER",
    )
    extrapolation: EnumProperty(
        name="Extrapolation",
        items=[
            ("CONSTANT", "Constant", "Hold the first/last value outside the keyed range"),
            ("LINEAR", "Linear", "Continue the slope outside the keyed range"),
        ],
        default="CONSTANT",
    )
    live_preview: BoolProperty(
        name="Live Preview",
        description="On: SDK takes effect. Off: SDK is disabled so you can inspect raw poses",
        default=True,
        update=_preview_update,
    )


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class SDK_OT_load_driver(Operator):
    bl_idname = "sdk.load_driver"
    bl_label = "Load Driver"
    bl_description = "Use the active object (or active bone in Pose Mode) as the driver"

    def execute(self, context):
        s = context.scene.sdk_settings
        obj = context.active_object
        if obj is None:
            self.report({"ERROR"}, "No active object")
            return {"CANCELLED"}
        bone = ""
        if context.mode == "POSE" and context.active_pose_bone:
            bone = context.active_pose_bone.name
        s.driver_obj = obj
        s.driver_bone = bone
        capture_driver_rest(s)
        return {"FINISHED"}


class SDK_OT_capture_driver_rest(Operator):
    bl_idname = "sdk.capture_driver_rest"
    bl_label = "Capture Driver Rest"
    bl_description = "Use the driver's current value as its rest value"

    def execute(self, context):
        capture_driver_rest(context.scene.sdk_settings)
        return {"FINISHED"}


class SDK_OT_driver_to_rest(Operator):
    bl_idname = "sdk.driver_to_rest"
    bl_label = "Driver to Rest"
    bl_description = "Set the driver channel back to its rest value"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if s.driver_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        rel, idx = driver_spec(s)
        try:
            set_by_path(s.driver_obj, full_path(s.driver_bone, rel), idx, s.driver_rest)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "Can't set driver value: %s" % e)
            return {"CANCELLED"}
        context.view_layer.update()
        return {"FINISHED"}


class SDK_OT_jump_key(Operator):
    bl_idname = "sdk.jump_key"
    bl_label = "Jump Key"
    bl_description = "Jump driver to previous or next keyed value"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=[("PREV", "Prev", ""), ("NEXT", "Next", "")])

    def execute(self, context):
        s = context.scene.sdk_settings
        if s.driver_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        rel, idx = driver_spec(s)
        path = full_path(s.driver_bone, rel)
        cur = get_value(s.driver_obj, path, idx)
        keys = get_all_keyed_values(s)
        if not keys:
            self.report({"INFO"}, "No keys found on driven channels")
            return {"CANCELLED"}

        target = None
        if self.direction == "PREV":
            prevs = [k for k in keys if k < cur - KEY_EPS]
            if prevs:
                target = prevs[-1]
            elif abs(keys[0] - cur) > KEY_EPS:
                target = keys[0]
        else:
            nexts = [k for k in keys if k > cur + KEY_EPS]
            if nexts:
                target = nexts[0]
            elif abs(keys[-1] - cur) > KEY_EPS:
                target = keys[-1]

        if target is None:
            self.report({"INFO"}, "No more keys in that direction")
            return {"CANCELLED"}

        set_by_path(s.driver_obj, path, idx, target)
        context.view_layer.update()
        self.report({"INFO"}, "Jumped to driver key: %.4f" % target)
        return {"FINISHED"}


class SDK_OT_channel_preset(Operator):
    bl_idname = "sdk.channel_preset"
    bl_label = "Channel Preset"
    bl_description = "Quick toggle channel selection presets"

    action: EnumProperty(items=[
        ("ALL", "All", ""),
        ("LOC", "Loc", ""),
        ("ROT", "Rot", ""),
        ("SCALE", "Scale", ""),
        ("CLEAR", "Clear", ""),
    ])

    def execute(self, context):
        s = context.scene.sdk_settings
        if self.action == "ALL":
            s.ch_loc = (True, True, True)
            s.ch_rot = (True, True, True)
            s.ch_scale = (True, True, True)
            if s.mode != "ADDITIVE":
                s.ch_quat = (True, True, True, True)
        elif self.action == "LOC":
            s.ch_loc = (True, True, True)
            s.ch_rot = (False, False, False)
            s.ch_scale = (False, False, False)
            s.ch_quat = (False, False, False, False)
        elif self.action == "ROT":
            s.ch_loc = (False, False, False)
            s.ch_rot = (True, True, True)
            s.ch_scale = (False, False, False)
            if s.mode != "ADDITIVE":
                s.ch_quat = (True, True, True, True)
        elif self.action == "SCALE":
            s.ch_loc = (False, False, False)
            s.ch_rot = (False, False, False)
            s.ch_scale = (True, True, True)
            s.ch_quat = (False, False, False, False)
        elif self.action == "CLEAR":
            s.ch_loc = (False, False, False)
            s.ch_rot = (False, False, False)
            s.ch_scale = (False, False, False)
            s.ch_quat = (False, False, False, False)
        return {"FINISHED"}


class SDK_OT_load_driven(Operator):
    bl_idname = "sdk.load_driven"
    bl_label = "Load Driven"
    bl_description = "Use selected objects (or selected bones in Pose Mode) as driven targets"

    append: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        s = context.scene.sdk_settings
        if context.mode == "POSE" and context.selected_pose_bones:
            entries = [(context.active_object, pb.name) for pb in context.selected_pose_bones]
        else:
            entries = [(o, "") for o in context.selected_objects]
        if not entries:
            self.report({"ERROR"}, "Nothing selected")
            return {"CANCELLED"}
        if not self.append:
            s.driven_items.clear()
        existing = {(i.obj.as_pointer() if i.obj else 0, i.bone) for i in s.driven_items}
        for obj, bone in entries:
            key = (obj.as_pointer(), bone)
            if key in existing:
                continue
            existing.add(key)
            item = s.driven_items.add()
            item.obj = obj
            item.bone = bone
            capture(item)
        return {"FINISHED"}


class SDK_OT_load_shape_keys(Operator):
    bl_idname = "sdk.load_shape_keys"
    bl_label = "Load Mesh Shape Keys"
    bl_description = "Add all shape keys of the active mesh to custom driven channels"

    def execute(self, context):
        s = context.scene.sdk_settings
        obj = context.active_object
        if not obj or not hasattr(obj, "data") or not hasattr(obj.data, "shape_keys") or not obj.data.shape_keys:
            self.report({"ERROR"}, "Active object has no shape keys")
            return {"CANCELLED"}
        basis = obj.data.shape_keys.reference_key
        keys = [k.name for k in obj.data.shape_keys.key_blocks if k != basis]
        if not keys:
            self.report({"INFO"}, "No non-basis shape keys found")
            return {"CANCELLED"}
        new_paths = ['data.shape_keys.key_blocks["%s"].value' % bpy.utils.escape_identifier(k) for k in keys]
        existing = [p.strip() for p in s.ch_custom.split(";") if p.strip()]
        for p in new_paths:
            if p not in existing:
                existing.append(p)
        s.ch_custom = ";".join(existing)
        self.report({"INFO"}, "Added %d shape key(s) to Custom driven channels" % len(keys))
        return {"FINISHED"}


class SDK_OT_remove_driven(Operator):
    bl_idname = "sdk.remove_driven"
    bl_label = "Remove Driven"
    bl_description = "Remove the highlighted entry from the driven list"

    def execute(self, context):
        s = context.scene.sdk_settings
        if 0 <= s.driven_index < len(s.driven_items):
            s.driven_items.remove(s.driven_index)
            s.driven_index = max(0, s.driven_index - 1)
        return {"FINISHED"}


class SDK_OT_capture_base(Operator):
    bl_idname = "sdk.capture_base"
    bl_label = "Capture Base"
    bl_description = "Re-capture current channel values of driven items as the additive base"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for item in context.scene.sdk_settings.driven_items:
            capture(item)
        return {"FINISHED"}


class SDK_OT_reset_base(Operator):
    bl_idname = "sdk.reset_base"
    bl_label = "Reset to Base"
    bl_description = "Put enabled driven channels back to captured base values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        specs = transform_specs(s)
        for item in s.driven_items:
            reset_base(item, specs)
        context.view_layer.update()
        return {"FINISHED"}


class SDK_OT_mirror_keys(Operator):
    bl_idname = "sdk.mirror_keys"
    bl_label = "Mirror / Flip to Symmetrical Bones"
    bl_description = "Copy and mirror SDK drivers and curves to counterpart bones (e.g. .L <-> .R)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if s.driver_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        d_rel, d_idx = driver_spec(s)
        d_path = full_path(s.driver_bone, d_rel)
        d_var_path = var_path(d_path, d_idx)

        def flip_name(name):
            patterns = [
                (r"\.L$", ".R"), (r"\.R$", ".L"),
                (r"_L$", "_R"), (r"_R$", "_L"),
                (r"\.left$", ".right"), (r"\.right$", ".left"),
                (r"_left$", "_right"), (r"_right$", "_left"),
            ]
            for pat, rep in patterns:
                new_n, c = re.subn(pat, rep, name, flags=re.IGNORECASE)
                if c > 0:
                    return new_n
            return ""

        mirrored = 0
        for item in s.driven_items:
            if not item.bone or not item.obj:
                continue
            opp_bone = flip_name(item.bone)
            if not opp_bone or opp_bone not in item.obj.pose.bones:
                continue

            for kind, axis in transform_specs(s):
                src_path = full_path(item.bone, DIRECT_ATTR[kind])
                dst_path = full_path(opp_bone, DIRECT_ATTR[kind])
                src_fc = find_driver_fcurve(item.obj, src_path, axis)
                if src_fc is None or not is_sdk_fcurve(src_fc):
                    continue

                flip_sign = -1.0 if (axis == 0 and kind in ("loc", "rot")) else 1.0
                dst_fc, msg = ensure_driver(item.obj, dst_path, axis, s.driver_obj, d_var_path, not s.live_preview)
                if dst_fc:
                    while len(dst_fc.keyframe_points):
                        dst_fc.keyframe_points.remove(dst_fc.keyframe_points[0], fast=True)
                    for kp in src_fc.keyframe_points:
                        key_curve(dst_fc, kp.co.x, kp.co.y * flip_sign, kp.interpolation, src_fc.extrapolation)
                    mirrored += 1

        context.view_layer.update()
        self.report({"INFO"}, "Mirrored %d channel curve(s) across symmetry" % mirrored)
        return {"FINISHED"}


class SDK_OT_key(Operator):
    bl_idname = "sdk.key"
    bl_label = "Key"
    bl_description = "Store driven channels against current driver value (creates drivers/curves if needed)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        drv_obj = s.driver_obj
        if drv_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        d_rel, d_idx = driver_spec(s)
        d_path = full_path(s.driver_bone, d_rel)
        d_var_path = var_path(d_path, d_idx)
        try:
            x = get_value(drv_obj, d_path, d_idx)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver property '%s'" % d_var_path)
            return {"CANCELLED"}

        if not len(s.driven_items):
            self.report({"ERROR"}, "Load driven objects first")
            return {"CANCELLED"}
        tspecs, qspecs, cspecs = transform_specs(s), quat_specs(s), custom_specs(s)
        if not (tspecs or qspecs or cspecs):
            self.report({"ERROR"}, "Enable at least one driven channel")
            return {"CANCELLED"}

        mode = s.mode
        problems = []
        if mode == "ADDITIVE" and qspecs:
            problems.append("Quaternion channels are Direct-mode only")

        keyed = 0
        action = get_action_for_driver(s) if mode == "ACTION" else None

        for item in s.driven_items:
            obj = item.obj
            if obj is None:
                continue
            if owner_of(item) is None:
                problems.append("'%s' not found" % (item.bone or obj.name))
                continue

            done = []
            for kind, axis in tspecs:
                label = "%s %s%s" % (item.bone or obj.name, kind, "XYZ"[axis])
                if mode == "ADDITIVE":
                    msg = safe_call(key_additive, label, s, item, kind, axis,
                                    drv_obj, d_path, d_idx, d_var_path, x)
                elif mode == "DIRECT":
                    msg = safe_call(key_direct, label, s, item, DIRECT_ATTR[kind], axis,
                                    drv_obj, d_path, d_idx, d_var_path, x)
                else:  # ACTION
                    msg = safe_call(key_action_mode, label, s, item, DIRECT_ATTR[kind], axis, action, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1
                    done.append((kind, axis))

            if mode != "ADDITIVE":
                for i in qspecs:
                    label = "%s quat %s" % (item.bone or obj.name, "WXYZ"[i])
                    if mode == "DIRECT":
                        msg = safe_call(key_direct, label, s, item, "rotation_quaternion", i,
                                        drv_obj, d_path, d_idx, d_var_path, x)
                    else:  # ACTION
                        msg = safe_call(key_action_mode, label, s, item, "rotation_quaternion", i, action, x)
                    if msg:
                        problems.append(msg)
                    else:
                        keyed += 1

            for rel, idx in cspecs:
                label = "%s %s" % (item.bone or obj.name, rel)
                msg = safe_call(key_direct, label, s, item, rel, idx, drv_obj, d_path, d_idx, d_var_path, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1

            if (mode == "ADDITIVE" or mode == "ACTION") and s.auto_reset and done:
                reset_base(item, done)

        if mode == "ACTION" and action:
            update_action_constraint_ranges(action, s)

        context.view_layer.update()
        if problems:
            self.report({"WARNING"}, "; ".join(sorted(set(problems))[:3]))
        if not keyed:
            return {"CANCELLED"}
        self.report({"INFO"}, "Keyed %d channel(s) at driver value %.4f" % (keyed, x))
        return {"FINISHED"}


class SDK_OT_delete_key(Operator):
    bl_idname = "sdk.delete_key"
    bl_label = "Delete Key at Driver Value"
    bl_description = "Remove key at the current driver value from enabled driven channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if s.driver_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        d_rel, d_idx = driver_spec(s)
        try:
            x = get_value(s.driver_obj, full_path(s.driver_bone, d_rel), d_idx)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver property")
            return {"CANCELLED"}

        removed = 0
        if s.mode == "ACTION":
            driver_name = s.driver_bone if s.driver_bone else s.driver_obj.name
            chan = s.driver_channel.replace(":", "_")
            act = bpy.data.actions.get("%s%s_%s" % (SDK_ACTION_PREFIX, driver_name, chan))
            if act:
                frame = x * 100.0
                for fc in list(act.fcurves):
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - frame) < KEY_EPS * 100.0:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                update_action_constraint_ranges(act, s)
        else:
            for item in s.driven_items:
                obj = item.obj
                if obj is None:
                    continue
                for path, idx in all_targets(s, item):
                    target_id, rel_path = resolve_id_and_path(obj, path)
                    fc = find_driver_fcurve(target_id, rel_path, idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - x) < KEY_EPS:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                            break
        context.view_layer.update()
        self.report({"INFO"}, "Removed %d key(s)" % removed)
        return {"FINISHED"}


class SDK_OT_delete_curves(Operator):
    bl_idname = "sdk.delete_curves"
    bl_label = "Delete Driven Curves"
    bl_description = "Remove driven-key curves or action constraints from enabled driven channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        removed = 0

        if s.mode == "ACTION":
            driver_name = s.driver_bone if s.driver_bone else (s.driver_obj.name if s.driver_obj else "Driver")
            chan = s.driver_channel.replace(":", "_")
            act = bpy.data.actions.get("%s%s_%s" % (SDK_ACTION_PREFIX, driver_name, chan))
            for item in s.driven_items:
                owner = owner_of(item)
                if owner and SDK_ACTION_CON in owner.constraints:
                    con = owner.constraints[SDK_ACTION_CON]
                    owner.constraints.remove(con)
                    removed += 1
            if act and act.users <= 1:
                bpy.data.actions.remove(act)
        else:
            for item in s.driven_items:
                obj = item.obj
                if obj is None:
                    continue
                for path, idx in all_targets(s, item):
                    target_id, rel_path = resolve_id_and_path(obj, path)
                    fc = find_driver_fcurve(target_id, rel_path, idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    try:
                        target_id.driver_remove(rel_path, idx) if idx >= 0 else target_id.driver_remove(rel_path)
                    except Exception:  # noqa: BLE001
                        continue
                    removed += 1
                    if path.startswith("delta_") or ".constraints[" in path:
                        try:
                            set_by_path(obj, path, idx, identity_for(path))
                        except Exception:  # noqa: BLE001
                            pass

                if item.bone and obj.pose and item.bone in obj.pose.bones:
                    pb = obj.pose.bones[item.bone]
                    drivers = list(obj.animation_data.drivers) if obj.animation_data else []
                    for name in SDK_CON.values():
                        con = pb.constraints.get(name)
                        if con is None:
                            continue
                        prefix = full_path(item.bone, 'constraints["%s"]' % name)
                        if not any(fc.data_path.startswith(prefix) for fc in drivers):
                            pb.constraints.remove(con)

        context.view_layer.update()
        self.report({"INFO"}, "Removed %d driven relationship(s)" % removed)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

class SDK_UL_driven(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item.obj is None:
            layout.label(text="<missing>", icon="ERROR")
        elif item.bone:
            layout.label(text="%s : %s" % (item.obj.name, item.bone), icon="BONE_DATA")
        else:
            layout.label(text=item.obj.name, icon="OBJECT_DATA")


class SDK_PT_main(Panel):
    bl_label = "Set Driven Key"
    bl_idname = "SDK_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Driven Key"

    def draw(self, context):
        layout = self.layout
        s = context.scene.sdk_settings

        # ---- Driver -------------------------------------------------------
        box = layout.box()
        box.label(text="Driver", icon="DRIVER")
        box.operator("sdk.load_driver", icon="EYEDROPPER")
        if s.driver_obj:
            name = s.driver_obj.name + (" : " + s.driver_bone if s.driver_bone else "")
            box.label(text=name, icon="BONE_DATA" if s.driver_bone else "OBJECT_DATA")
            box.prop(s, "driver_channel", text="")
            if s.driver_channel == "CUSTOM":
                box.prop(s, "driver_custom")
            box.prop(s, "driver_scrub", text="Scrub Value", slider=True)
            row = box.row(align=True)
            row.prop(s, "driver_rest", text="Rest")
            row.operator("sdk.capture_driver_rest", text="", icon="PINNED")
            row.operator("sdk.driver_to_rest", text="", icon="LOOP_BACK")

            # Keyframe navigation helpers
            keys = get_all_keyed_values(s)
            row = box.row(align=True)
            row.operator("sdk.jump_key", text="Prev Key", icon="PREV_KEYFRAME").direction = "PREV"
            row.operator("sdk.jump_key", text="Next Key", icon="NEXT_KEYFRAME").direction = "NEXT"
            if keys:
                box.label(text="Keys: %s" % ", ".join("%.2f" % k for k in keys[:6]), icon="KEYFRAME")
            else:
                box.label(text="No keys stored on channels yet", icon="INFO")
        else:
            box.label(text="No driver loaded", icon="INFO")

        # ---- Driven -------------------------------------------------------
        box = layout.box()
        box.label(text="Driven", icon="LINKED")
        row = box.row(align=True)
        row.operator("sdk.load_driven", text="Load Driven", icon="EYEDROPPER").append = False
        row.operator("sdk.load_driven", text="Add", icon="ADD").append = True
        row = box.row()
        row.template_list("SDK_UL_driven", "", s, "driven_items", s, "driven_index", rows=3)
        row.operator("sdk.remove_driven", text="", icon="REMOVE")

        # Channel presets
        row = box.row(align=True)
        row.label(text="Presets:")
        row.operator("sdk.channel_preset", text="All").action = "ALL"
        row.operator("sdk.channel_preset", text="Loc").action = "LOC"
        row.operator("sdk.channel_preset", text="Rot").action = "ROT"
        row.operator("sdk.channel_preset", text="Scale").action = "SCALE"
        row.operator("sdk.channel_preset", text="Clear").action = "CLEAR"

        col = box.column(align=True)
        for label, attr, comps in (
            ("Location", "ch_loc", "XYZ"),
            ("Rotation", "ch_rot", "XYZ"),
            ("Quat", "ch_quat", "WXYZ"),
            ("Scale", "ch_scale", "XYZ"),
        ):
            split = col.split(factor=0.3, align=True)
            split.label(text=label)
            sub = split.row(align=True)
            sub.enabled = not (attr == "ch_quat" and s.mode == "ADDITIVE")
            for i, c in enumerate(comps):
                sub.prop(s, attr, index=i, text=c, toggle=True)
        box.prop(s, "ch_custom", text="Custom")
        box.operator("sdk.load_shape_keys", text="Add Mesh Shape Keys", icon="SHAPEKEY_DATA")

        # ---- Mode ---------------------------------------------------------
        layout.prop(s, "mode")
        layout.prop(s, "auto_rest_key")
        if s.mode == "ADDITIVE":
            row = layout.row(align=True)
            row.operator("sdk.capture_base", icon="PINNED")
            row.operator("sdk.reset_base", icon="LOOP_BACK")
            layout.prop(s, "auto_reset")

        # ---- Actions ------------------------------------------------------
        row = layout.row(align=True)
        row.scale_y = 1.6
        row.operator("sdk.key", icon="KEY_HLT")
        row = layout.row(align=True)
        row.operator("sdk.delete_key", text="Delete Key", icon="KEY_DEHLT")
        row.operator("sdk.delete_curves", text="Delete Curves", icon="TRASH")
        layout.operator("sdk.mirror_keys", text="Mirror .L / .R Poses", icon="MOD_MIRROR")
        layout.prop(
            s, "live_preview", toggle=True,
            icon="HIDE_OFF" if s.live_preview else "HIDE_ON",
        )


class SDK_PT_options(Panel):
    bl_label = "Key Options"
    bl_idname = "SDK_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Driven Key"
    bl_parent_id = "SDK_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = context.scene.sdk_settings
        col = self.layout.column()
        col.prop(s, "interpolation")
        col.prop(s, "extrapolation")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

classes = (
    SDK_DrivenItem,
    SDK_Settings,
    SDK_OT_load_driver,
    SDK_OT_capture_driver_rest,
    SDK_OT_driver_to_rest,
    SDK_OT_jump_key,
    SDK_OT_channel_preset,
    SDK_OT_load_driven,
    SDK_OT_load_shape_keys,
    SDK_OT_remove_driven,
    SDK_OT_capture_base,
    SDK_OT_reset_base,
    SDK_OT_mirror_keys,
    SDK_OT_key,
    SDK_OT_delete_key,
    SDK_OT_delete_curves,
    SDK_UL_driven,
    SDK_PT_main,
    SDK_PT_options,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)


def unregister():
    del bpy.types.Scene.sdk_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
