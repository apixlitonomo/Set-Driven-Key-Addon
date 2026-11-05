# Set Driven Key Addon for Blender
# Version 1.4.1 — bugfixes and robustness:
# - Fixed set_by_path for array indices and bone-relative paths (scrub / jump / rest)
# - find_driver_fcurve respects index=-1 for non-array properties
# - Mirror operator supports Additive constraints and Action Constraint modes
# - Load shape keys also adds the mesh to driven_items
# - Action Constraint + custom drivers use evaluation_time when available, else documented frame mapping
# - Rest-key insertion is robust (never skipped on first key)
# - Dummy SDK_Constraint_Target cleaned up when unused
# - Live Preview mutes Action constraints and shape-key drivers consistently
# - Configurations store richer target identity; clearer load warnings
# - Mode-conflict warnings when keying
# - Cached keyed-value list for UI performance
#
bl_info = {
    "name": "Set Driven Key",
    "author": "OceanMan, CloudKing",
    "version": (1, 4, 1),
    "blender": (3, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Driven Key",
    "description": "Professional Set Driven Key for Blender with Configuration Manager, Multi-Axis Stacking, Additive, Direct, and Action Constraint modes",
    "category": "Animation",
}

import json
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

# Name of the driver variable the add-on creates.
SDK_VAR = "sdk_driver"

# Constraint naming prefixes
SDK_CON_BASE = {"loc": "SDK Location", "rot": "SDK Rotation", "scale": "SDK Scale"}
CON_SUFFIX = {"loc": "", "rot": "_rot", "scale": "_scale"}
DIRECT_ATTR = {"loc": "location", "rot": "rotation_euler", "scale": "scale"}

# Action constraint naming
SDK_ACTION_PREFIX = "SDK_Action_"

KEY_EPS = 0.01  # Blender treats keys closer than this (on the X axis) as the same key

DUMMY_TARGET_NAME = "SDK_Constraint_Target"

# Cache for keyed driver values (invalidated on key / delete / mode change)
_keyed_values_cache = {"key": None, "values": []}


# --------------------------------------------------------------------------
# Path / value helpers
# --------------------------------------------------------------------------

_CUSTOM_RE = re.compile(r"^(.+)\[(\d+)\]$")
_BPY_DATA_RE = re.compile(r"^bpy\.data\.([a-zA-Z0-9_]+)\[[\"\']([^\"\']+)[\"\']\]\.(.+)$")
_BONE_SUBPATH_RE = re.compile(r"^pose\.bones\[[\"\']([^\"\']+)[\"\']\]\.(.+)$")
# Trailing array index on a normal RNA path: ...location[0] or ...scale[2]
_ARRAY_TAIL_RE = re.compile(r"^(.*)\[(\d+)\]$")


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


def resolve_driver_target(s):
    """Resolve the effective ID block, bone name, relative attribute path, array index,

    and driver variable data path for the current driver setting.
    Handles standard channels, relative custom paths, and full 'bpy.data...' paths.
    Returns (drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key).
    """
    layer = getattr(s, "config_layer_tag", "").strip()
    layer_tag = ("_" + re.sub(r"[^a-zA-Z0-9_]", "_", layer)) if layer else ""

    if s.driver_channel == "CUSTOM":
        text = s.driver_custom.strip()
        m_bpy = _BPY_DATA_RE.match(text)
        if m_bpy:
            coll_name, id_name, subpath = m_bpy.groups()
            coll = getattr(bpy.data, coll_name, None)
            drv_id = coll.get(id_name) if coll else None
            m_bone = _BONE_SUBPATH_RE.match(subpath)
            drv_bone = m_bone.group(1) if m_bone else ""
            rel_attr_raw = m_bone.group(2) if m_bone else subpath
            rel_attr, idx = parse_custom(rel_attr_raw)
            d_var_path = subpath
            clean_sub = re.sub(r"[^a-zA-Z0-9_]", "_", subpath).strip("_")
            scope_key = "%s_%s%s" % (id_name, clean_sub, layer_tag)
            return drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key

        rel_attr, idx = parse_custom(text)
        drv_id = s.driver_obj
        drv_bone = s.driver_bone
        d_var_path = full_path(drv_bone, var_path(rel_attr, idx))
        clean_sub = re.sub(r"[^a-zA-Z0-9_]", "_", d_var_path).strip("_")
        b_name = drv_bone or (drv_id.name if drv_id else "obj")
        scope_key = "%s_%s%s" % (b_name, clean_sub, layer_tag)
        return drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key

    attr, idx = s.driver_channel.split(":")
    idx = int(idx)
    drv_id = s.driver_obj
    drv_bone = s.driver_bone
    d_var_path = full_path(drv_bone, var_path(attr, idx))
    b_name = drv_bone or (drv_id.name if drv_id else "obj")
    scope_key = "%s_%s%s" % (b_name, s.driver_channel.replace(":", "_"), layer_tag)
    return drv_id, drv_bone, attr, idx, d_var_path, scope_key


def resolve_id_and_path(obj, path):
    """Determine the host ID block and relative path for driving."""
    if path.startswith("data.shape_keys."):
        if obj and hasattr(obj, "data") and hasattr(obj.data, "shape_keys") and obj.data.shape_keys:
            return obj.data.shape_keys, path[len("data.shape_keys."):]
    return obj, path


def get_value(obj, path, index):
    id_block, rel_path = resolve_id_and_path(obj, path)
    # Support paths that already embed the array index (e.g. location[0])
    m = _ARRAY_TAIL_RE.match(rel_path)
    if m and not rel_path.rstrip("]").endswith('"') and not rel_path.rstrip("]").endswith("'"):
        # Only treat as array tail when the bracket is numeric and not an ID-prop name
        try:
            tail_idx = int(m.group(2))
            base = m.group(1)
            value = id_block.path_resolve(base)
            return float(value[tail_idx] if index < 0 else value[index if index >= 0 else tail_idx])
        except (ValueError, TypeError, IndexError, KeyError):
            pass
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

    Correctly handles:
    - Array elements already embedded in the path (location[0], rotation_euler[1], …)
    - Full relative bone paths (pose.bones["Bone"].location[0])
    - ID properties (["prop"], pose.bones["B"]["prop"])
    - Shape-key paths (data.shape_keys.key_blocks["Name"].value)
    """
    id_block, rel_path = resolve_id_and_path(obj, path)

    # 1) ID properties: ["prop"] or parent["prop"][i]
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

    # 2) Path that ends with a numeric array index: …location[0]
    m_arr = _ARRAY_TAIL_RE.match(rel_path)
    if m_arr:
        base_path = m_arr.group(1)
        arr_index = int(m_arr.group(2))
        # Prefer explicit index argument when provided
        use_idx = index if index >= 0 else arr_index
        if "." in base_path:
            parent, prop = base_path.rsplit(".", 1)
            owner = id_block.path_resolve(parent) if parent else id_block
        else:
            owner, prop = id_block, base_path
        getattr(owner, prop)[use_idx] = value
        return

    # 3) Ordinary RNA attribute, optional separate index
    if "." in rel_path:
        parent, prop = rel_path.rsplit(".", 1)
        owner = id_block.path_resolve(parent) if parent else id_block
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
        'Any data path, e.g. ["myprop"], location[2] or bpy.data.objects["Rig"].pose.bones["Claw"].scale[0]',
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
    try:
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            return
        val = drv_id.path_resolve(d_var_path)
        s.driver_rest = float(val)
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
        empty.hide_select = True
        # Prefer a dedicated hidden collection so the empty is easy to ignore
        coll = bpy.data.collections.get("SDK_Internal")
        if coll is None:
            coll = bpy.data.collections.new("SDK_Internal")
            coll.hide_viewport = True
            coll.hide_render = True
            scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
            if scene:
                try:
                    scene.collection.children.link(coll)
                except RuntimeError:
                    pass
        if coll and empty.name not in coll.objects:
            try:
                coll.objects.link(empty)
            except RuntimeError:
                scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
                if scene and scene.collection:
                    scene.collection.objects.link(empty)
        else:
            scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
            if scene and scene.collection and empty.name not in scene.collection.objects:
                scene.collection.objects.link(empty)
    return empty


def cleanup_dummy_target():
    """Remove SDK_Constraint_Target if no constraints still reference it."""
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        return
    for obj in bpy.data.objects:
        for con in getattr(obj, "constraints", []) or []:
            if getattr(con, "target", None) == empty:
                return
        if obj.pose:
            for pb in obj.pose.bones:
                for con in pb.constraints:
                    if getattr(con, "target", None) == empty:
                        return
    # Safe to remove
    try:
        bpy.data.objects.remove(empty, do_unlink=True)
    except Exception:  # noqa: BLE001
        pass
    coll = bpy.data.collections.get("SDK_Internal")
    if coll and len(coll.objects) == 0 and len(coll.children) == 0:
        try:
            bpy.data.collections.remove(coll)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Driver / curve helpers (High performance O(1) lookups)
# --------------------------------------------------------------------------

def find_driver_fcurve(target_id, path, index):
    """Fast driver lookup using drivers.find(path, index).

    Matches Blender semantics: for non-array properties pass index=-1 (or omit).
    Do not force index to 0 when the property is scalar.
    """
    ad = getattr(target_id, "animation_data", None)
    if ad is None:
        return None
    try:
        if index < 0:
            # Scalar / non-array property
            return ad.drivers.find(path)
        return ad.drivers.find(path, index=index)
    except (TypeError, AttributeError):
        pass
    # Fallback linear scan
    for fc in ad.drivers:
        if fc.data_path == path:
            if index < 0 or fc.array_index == index:
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
            try:
                target_id.driver_remove(rel_path)
            except Exception:  # noqa: BLE001
                pass
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
    """Ensure the rest key exists on the curve before (or with) the posed key.

    Robust against an empty curve or a curve that only contains the key about to
    be written: always insert rest at driver_rest when auto_rest_key is on and
    the rest value is known and distinct from the current key.
    """
    if rest_y is None or not s.auto_rest_key:
        return
    if abs(x - s.driver_rest) < KEY_EPS:
        return
    # Insert rest key if missing (regardless of whether a posed key already exists)
    has_rest = any(abs(k.co.x - s.driver_rest) < KEY_EPS for k in fc.keyframe_points)
    if not has_rest:
        key_curve(fc, s.driver_rest, rest_y, s.interpolation, s.extrapolation)


def safe_call(fn, label, *args):
    """Run one key operation. A failure becomes a message instead of aborting everything."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return "%s failed: %s" % (label, e)


def invalidate_keyed_cache():
    _keyed_values_cache["key"] = None
    _keyed_values_cache["values"] = []


# --------------------------------------------------------------------------
# Additive mode helpers (Multi-axis scoped constraints)
# --------------------------------------------------------------------------

def constraint_name_for(kind, scope_key):
    """Unique constraint name per configuration and driver axis/channel."""
    return "%s [%s]" % (SDK_CON_BASE[kind], scope_key)


def additive_targets(item, kind, axis, scope_key):
    """(path, index) pairs the SDK drives for one transform channel in additive mode."""
    con_name = constraint_name_for(kind, scope_key)
    a = "xyz"[axis]
    rel = 'constraints["%s"].' % con_name
    return [
        (full_path(item.bone, rel + "to_%s_%s%s" % (m, a, CON_SUFFIX[kind])), -1)
        for m in ("min", "max")
    ]


def ensure_constraint(item, kind, scope_key, mute):
    """Ensure a Transformation constraint exists on the pose bone or object."""
    owner = owner_of(item)
    con_name = constraint_name_for(kind, scope_key)
    con = owner.constraints.get(con_name)
    if con is None:
        con = owner.constraints.new("TRANSFORM")
        con.name = con_name
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


def key_additive(s, item, kind, axis, drv_obj, d_path, d_idx, d_var_path, scope_key, x):
    """Key one transform channel as an additive offset. Supports multiple driver axes."""
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
        base_off = current_euler(owner)[axis] - rest_euler(item, owner)[axis]
        if mode == "QUATERNION":
            base_off = (base_off + math.pi) % (2.0 * math.pi) - math.pi
    else:
        rest = item.rest_scale[axis]
        if abs(rest) < 1e-8:
            return "Base scale is zero on %s, can't compute a ratio" % obj.name
        base_off = owner.scale[axis] / rest

    targets = additive_targets(item, kind, axis, scope_key)
    for path, p_idx in targets:
        if obj == drv_obj and path == d_path and max(p_idx, 0) == max(d_idx, 0):
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

    try:
        ensure_constraint(item, kind, scope_key, not s.live_preview)
    except Exception as e:  # noqa: BLE001
        return "Can't set up constraint on '%s': %s" % (item.bone or obj.name, e)

    for path, p_idx in targets:
        fc, msg = ensure_driver(obj, path, p_idx, drv_obj, d_var_path, not s.live_preview)
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
# Action Constraint Mode Helpers (Multi-axis & dynamic stacking support)
# --------------------------------------------------------------------------

def action_transform_channel(s):
    """Map driver channel to Action Constraint transform_channel enum identifier.

    Transform channels (location / rotation_euler / scale) keep their natural mapping.
    Custom / non-transform drivers return None so callers can switch to evaluation_time
    (or the documented frame mapping: driver value V → frame V * 100).
    """
    if s.driver_channel.startswith("location:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")[idx]
    if s.driver_channel.startswith("rotation_euler:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("ROTATION_X", "ROTATION_Y", "ROTATION_Z")[idx]
    if s.driver_channel.startswith("scale:"):
        idx = int(s.driver_channel.split(":")[1])
        return ("SCALE_X", "SCALE_Y", "SCALE_Z")[idx]
    if s.driver_channel == "CUSTOM":
        text = s.driver_custom.strip()
        m = re.match(r"^(?:.*?\.)?(location|rotation_euler|scale)\[([0-2])\]$", text)
        if m:
            tr_type, idx_str = m.groups()
            comp = ("X", "Y", "Z")[int(idx_str)]
            prefix = "ROTATION" if "rot" in tr_type else tr_type.upper()
            return "%s_%s" % (prefix, comp)
        # Non-transform custom path → no transform channel
        return None
    return None


def action_constraint_name_for(scope_key):
    return "SDK Action [%s]" % scope_key


def get_action_for_driver(s, scope_key):
    """Return or create a dedicated Action for the current driver in Action Constraint mode."""
    act_name = "%s%s" % (SDK_ACTION_PREFIX, scope_key)
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


def _drive_constraint_eval_time(con, drv_obj, drv_data_path, mute):
    """Drive Action Constraint evaluation_time from the SDK driver when the API supports it.

    Mapping (documented): driver value V → evaluation uses frame = V * 100
    (same scale used by key_action_mode). min/max on the constraint still map
    the transform range when evaluation_time is unavailable.
    """
    if not hasattr(con, "evaluation_time") and not hasattr(con, "eval_time"):
        return False
    # Prefer evaluation_time (Blender naming varies slightly across versions)
    prop = "evaluation_time" if hasattr(con, "evaluation_time") else "eval_time"
    if hasattr(con, "use_eval_time"):
        try:
            con.use_eval_time = True
        except Exception:  # noqa: BLE001
            pass
    # Attach a driver on the constraint property itself
    owner_id = con.id_data
    # Path relative to the ID that owns the constraint
    # For pose bones: pose.bones["Bone"].constraints["Name"].evaluation_time
    try:
        data_path = con.path_from_id(prop)
    except Exception:  # noqa: BLE001
        return False
    try:
        fc = owner_id.driver_add(data_path)
        if isinstance(fc, (list, tuple)):
            return False
        fc.mute = mute
        drv = fc.driver
        for v in list(drv.variables):
            drv.variables.remove(v)
        var = drv.variables.new()
        var.name = SDK_VAR
        var.type = "SINGLE_PROP"
        t = var.targets[0]
        t.id_type = "OBJECT"
        if getattr(drv_obj, "rna_type", None) and drv_obj.rna_type.name.upper() == "KEY":
            t.id_type = "KEY"
        t.id = drv_obj
        t.data_path = drv_data_path
        # Scale: frame = driver_value * 100
        drv.type = "SCRIPTED"
        drv.expression = "%s * 100.0" % SDK_VAR
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_action_constraint(s, item, action, scope_key, drv_obj, drv_bone, d_var_path=""):
    """Ensure an Action constraint exists on the target bone or object for this driver axis.

    For transform-channel drivers the constraint reads the target transform.
    For custom / non-transform drivers we prefer evaluation_time (when available);
    otherwise LOCATION_X is used and the documented mapping driver→frame (V*100) applies
    via constraint min/max + frame_start/end updated by update_action_constraint_ranges.
    """
    owner = owner_of(item)
    if owner is None:
        return None, "Target owner not found"
    con_name = action_constraint_name_for(scope_key)
    con = owner.constraints.get(con_name)
    if con is None:
        con = owner.constraints.new("ACTION")
        con.name = con_name
        con.target_space = "LOCAL"
        if hasattr(con, "mix_mode"):
            try:
                con.mix_mode = "BEFORE_SPLIT"
            except Exception:  # noqa: BLE001
                pass
        if hasattr(con, "show_expanded"):
            con.show_expanded = False
    con.target = drv_obj
    con.subtarget = drv_bone or ""
    con.action = action
    channel = action_transform_channel(s)
    if channel is None:
        # Custom / non-transform: try evaluation_time driver, fall back to LOCATION_X
        used_eval = False
        if d_var_path:
            used_eval = _drive_constraint_eval_time(con, drv_obj, d_var_path, not s.live_preview)
        if not used_eval:
            con.transform_channel = "LOCATION_X"
    else:
        con.transform_channel = channel
    con.mute = not s.live_preview
    return con, ""


def update_action_constraint_ranges(action, s, scope_key):
    """Update min/max and frame_start/end across constraints using this action."""
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

    con_name = action_constraint_name_for(scope_key)
    seen_owners = set()
    for item in s.driven_items:
        owner = owner_of(item)
        if owner is None or owner.as_pointer() in seen_owners:
            continue
        seen_owners.add(owner.as_pointer())
        con = owner.constraints.get(con_name)
        if con and con.action == action:
            con.frame_start = int(round(min_frame))
            con.frame_end = int(round(max_frame))
            con.min = val_min
            con.max = val_max


def key_action_mode(s, item, attr, idx, action, scope_key, drv_obj, drv_bone, x, d_var_path=""):
    """Key one channel into the Action for Action Constraint mode."""
    obj = item.obj
    if item.bone and obj == drv_obj and item.bone == drv_bone:
        return "Bone '%s' can't drive itself" % item.bone
    path = full_path(item.bone, attr)
    try:
        y = get_value(obj, path, idx)
    except Exception:  # noqa: BLE001
        return "Can't read '%s'" % var_path(path, idx)

    con, msg = ensure_action_constraint(s, item, action, scope_key, drv_obj, drv_bone, d_var_path)
    if con is None:
        return msg

    frame = x * 100.0
    # Use index=-1 semantics correctly for non-array paths
    find_idx = max(idx, 0) if idx >= 0 else 0
    fc = action.fcurves.find(path, index=find_idx)
    if fc is None:
        try:
            fc = action.fcurves.new(path, index=find_idx) if idx >= 0 else action.fcurves.new(path)
        except Exception as e:  # noqa: BLE001
            return "Can't create action fcurve '%s': %s" % (path, e)

    if s.auto_rest_key and abs(x - s.driver_rest) >= KEY_EPS:
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
            has_rest = any(abs(k.co.x - rest_frame) < KEY_EPS * 100.0 for k in fc.keyframe_points)
            if not has_rest:
                key_curve(fc, rest_frame, rest_y, s.interpolation, s.extrapolation)

    key_curve(fc, frame, y, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------

def all_targets(s, item, scope_key=""):
    """Every (path, index) the enabled channels could be driven through."""
    out = []
    for kind, axis in transform_specs(s):
        out.append((full_path(item.bone, DIRECT_ATTR[kind]), axis))
        if scope_key:
            out.extend(additive_targets(item, kind, axis, scope_key))
    for i in quat_specs(s):
        out.append((full_path(item.bone, "rotation_quaternion"), i))
    for rel, idx in custom_specs(s):
        out.append((full_path(item.bone, rel), idx))
    return out


def get_all_keyed_values(s):
    """Return sorted unique list of keyed driver values for currently enabled driven channels.

    Results are cached and invalidated when keys are added/removed or the mode/driver changes.
    """
    drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
    if drv_id is None:
        return []
    cache_key = (drv_id.as_pointer(), d_var_path, s.mode, scope_key,
                 tuple(s.ch_loc), tuple(s.ch_rot), tuple(s.ch_scale), tuple(s.ch_quat), s.ch_custom)
    if _keyed_values_cache["key"] == cache_key:
        return _keyed_values_cache["values"]

    values = set()
    if s.mode == "ACTION":
        act = bpy.data.actions.get("%s%s" % (SDK_ACTION_PREFIX, scope_key))
        if act:
            for fc in act.fcurves:
                for kp in fc.keyframe_points:
                    values.add(round(kp.co.x / 100.0, 4))
        result = sorted(values)
        _keyed_values_cache["key"] = cache_key
        _keyed_values_cache["values"] = result
        return result

    for item in s.driven_items:
        obj = item.obj
        if obj is None:
            continue
        for path, p_idx in all_targets(s, item, scope_key):
            target_id, rel_path = resolve_id_and_path(obj, path)
            fc = find_driver_fcurve(target_id, rel_path, p_idx)
            if fc and is_sdk_fcurve(fc):
                for kp in fc.keyframe_points:
                    values.add(round(kp.co.x, 4))
    result = sorted(values)
    _keyed_values_cache["key"] = cache_key
    _keyed_values_cache["values"] = result
    return result


# --------------------------------------------------------------------------
# Configuration Data & Helpers
# --------------------------------------------------------------------------

class SDK_ConfigItem(PropertyGroup):
    name: StringProperty(name="Name", default="SDK_Config")
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_bone: StringProperty()
    driver_channel: StringProperty(default="location:0")
    driver_custom: StringProperty()
    driver_rest: FloatProperty(default=0.0)
    mode: StringProperty(default="ADDITIVE")
    config_layer_tag: StringProperty(default="")
    ch_loc: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_rot: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_quat: BoolVectorProperty(size=4, default=(False,) * 4)
    ch_scale: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_custom: StringProperty(default="")
    # Serialized driven targets: list of dicts with name + optional data/library fallbacks
    driven_data: StringProperty(default="[]")


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
                if not is_sdk_fcurve(fc):
                    continue
                fc.mute = not on
        # Shape-key data blocks
        if hasattr(obj, "data") and hasattr(obj.data, "shape_keys") and obj.data.shape_keys:
            sk_ad = obj.data.shape_keys.animation_data
            if sk_ad:
                for fc in sk_ad.drivers:
                    if is_sdk_fcurve(fc):
                        fc.mute = not on
        if obj.pose:
            for pb in obj.pose.bones:
                for con in pb.constraints:
                    # Additive ("SDK Location/Rotation/Scale …") and Action ("SDK Action […]")
                    if con.name.startswith("SDK ") or con.name.startswith("SDK Action"):
                        con.mute = not on
        for con in obj.constraints:
            if con.name.startswith("SDK ") or con.name.startswith("SDK Action"):
                con.mute = not on


def _driver_changed(self, context):
    capture_driver_rest(self)
    invalidate_keyed_cache()


def _driver_current_get(self):
    try:
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(self)
        if drv_id is None:
            return 0.0
        val = drv_id.path_resolve(d_var_path)
        return float(val)
    except Exception:  # noqa: BLE001
        return 0.0


def _driver_current_set(self, value):
    try:
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(self)
        if drv_id is None:
            return
        # d_var_path may already embed [index]; pass -1 so set_by_path parses the path
        set_by_path(drv_id, d_var_path, -1, value)
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
    # Configurations
    configs: CollectionProperty(type=SDK_ConfigItem)
    config_index: IntProperty(default=0)
    config_layer_tag: StringProperty(
        name="Layer Tag",
        description="Unique tag for this configuration layer. When set, creates a new stacked "
                    "transform constraint / action layer instead of overwriting existing ones",
        default="",
    )

    # Driver
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_bone: StringProperty()
    driver_channel: EnumProperty(
        name="Channel", items=DRIVER_ITEMS, default="location:0",
        description="Driver channel", update=_driver_changed,
    )
    driver_custom: StringProperty(
        name="Path",
        description='Data path, e.g. ["myprop"], location[2] or bpy.data.objects["Rig"].pose.bones["Claw"].scale[0]',
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
            ("ADDITIVE", "Additive (Multi-Axis)",
             "Non-destructive: driven offsets stack on top of base animation. "
             "Allows multiple driver axes, bones, and layers to drive channels simultaneously"),
            ("DIRECT", "Direct",
             "Maya-style: driver takes over channels directly"),
            ("ACTION", "Action Constraint",
             "Professional rigging: driver drives an Action Constraint eval range. "
             "Great for complex multi-bone pose sets, facial shapes, and high viewport performance"),
        ],
        default="ADDITIVE",
        update=lambda self, ctx: invalidate_keyed_cache(),
    )
    auto_reset: BoolProperty(
        name="Reset to Base after Key",
        description="After keying, put posed channels back to the captured base (Additive & Action modes)",
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
# Configuration Operators
# --------------------------------------------------------------------------

def _serialize_driven_item(item):
    """Store object name plus data-block / library fallbacks for robust restore."""
    obj = item.obj
    if obj is None:
        return {"name": "", "bone": item.bone, "data": "", "library": ""}
    data_name = ""
    lib_path = ""
    if obj.data:
        data_name = obj.data.name
        if obj.data.library:
            lib_path = obj.data.library.filepath
    if obj.library:
        lib_path = obj.library.filepath or lib_path
    return {
        "name": obj.name,
        "bone": item.bone,
        "data": data_name,
        "library": lib_path,
    }


def _resolve_driven_entry(entry):
    """Resolve a serialized driven entry to an Object (or None)."""
    if isinstance(entry, (list, tuple)):
        # Legacy format: [obj_name, bone_name]
        obj_name = entry[0] if entry else ""
        bone = entry[1] if len(entry) > 1 else ""
        obj = bpy.data.objects.get(obj_name)
        return obj, bone
    name = entry.get("name", "")
    bone = entry.get("bone", "")
    data_name = entry.get("data", "")
    lib_path = entry.get("library", "")
    obj = bpy.data.objects.get(name)
    if obj is not None:
        return obj, bone
    # Fallback: match by data name / library
    if data_name:
        for o in bpy.data.objects:
            if o.data and o.data.name == data_name:
                if not lib_path or (o.library and o.library.filepath == lib_path):
                    return o, bone
    return None, bone


class SDK_OT_config_add(Operator):
    bl_idname = "sdk.config_add"
    bl_label = "Save Current as Configuration"
    bl_description = "Save current driver, driven targets, channels, and layer tag as a named configuration"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Name", default="New_SDK_Config")

    def execute(self, context):
        s = context.scene.sdk_settings
        cfg = s.configs.add()
        cfg.name = self.name or ("Config_%d" % len(s.configs))
        cfg.driver_obj = s.driver_obj
        cfg.driver_bone = s.driver_bone
        cfg.driver_channel = s.driver_channel
        cfg.driver_custom = s.driver_custom
        cfg.driver_rest = s.driver_rest
        cfg.mode = s.mode
        cfg.config_layer_tag = s.config_layer_tag or cfg.name
        cfg.ch_loc = s.ch_loc
        cfg.ch_rot = s.ch_rot
        cfg.ch_quat = s.ch_quat
        cfg.ch_scale = s.ch_scale
        cfg.ch_custom = s.ch_custom
        driven_list = [_serialize_driven_item(it) for it in s.driven_items]
        cfg.driven_data = json.dumps(driven_list)
        s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Saved configuration '%s'" % cfg.name)
        return {"FINISHED"}

    def invoke(self, context, event):
        s = context.scene.sdk_settings
        b_name = s.driver_bone or (s.driver_obj.name if s.driver_obj else "Rig")
        chan = s.driver_channel.replace(":", "_")
        self.name = "%s_%s" % (b_name, chan)
        return context.window_manager.invoke_props_dialog(self)


class SDK_OT_config_load(Operator):
    bl_idname = "sdk.config_load"
    bl_label = "Load Configuration"
    bl_description = "Restore driver, targets, channels, and layer settings from selected configuration"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if not (0 <= s.config_index < len(s.configs)):
            self.report({"ERROR"}, "No configuration selected")
            return {"CANCELLED"}
        cfg = s.configs[s.config_index]
        s.driver_obj = cfg.driver_obj
        s.driver_bone = cfg.driver_bone
        s.driver_channel = cfg.driver_channel
        s.driver_custom = cfg.driver_custom
        s.driver_rest = cfg.driver_rest
        s.mode = cfg.mode
        s.config_layer_tag = cfg.config_layer_tag
        s.ch_loc = cfg.ch_loc
        s.ch_rot = cfg.ch_rot
        s.ch_quat = cfg.ch_quat
        s.ch_scale = cfg.ch_scale
        s.ch_custom = cfg.ch_custom

        missing = []
        try:
            driven_list = json.loads(cfg.driven_data)
            s.driven_items.clear()
            for entry in driven_list:
                obj, bone = _resolve_driven_entry(entry)
                if obj is None:
                    if isinstance(entry, dict):
                        missing.append(entry.get("name", "?"))
                    elif isinstance(entry, (list, tuple)) and entry:
                        missing.append(str(entry[0]))
                    continue
                item = s.driven_items.add()
                item.obj = obj
                item.bone = bone
                capture(item)
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, "Could not fully restore driven targets: %s" % e)

        invalidate_keyed_cache()
        if missing:
            self.report({"WARNING"}, "Missing driven object(s): %s" % ", ".join(missing[:5]))
        self.report({"INFO"}, "Loaded configuration '%s'" % cfg.name)
        return {"FINISHED"}


class SDK_OT_config_remove(Operator):
    bl_idname = "sdk.config_remove"
    bl_label = "Remove Configuration"
    bl_description = "Remove the selected configuration from the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if 0 <= s.config_index < len(s.configs):
            s.configs.remove(s.config_index)
            s.config_index = max(0, s.config_index - 1)
        return {"FINISHED"}


class SDK_UL_configs(UIList):
    """UIList for saved SDK Configurations with live search and filtering."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False, icon="SCRIPTPLUGINS")
            tag_text = ("[" + item.config_layer_tag + "]") if item.config_layer_tag else ""
            row.label(text=tag_text)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text=item.name, icon="SCRIPTPLUGINS")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []
        if self.filter_name:
            flt_flags = [
                self.bitflag_filter_item
                if (self.filter_name.lower() in item.name.lower() or self.filter_name.lower() in item.config_layer_tag.lower())
                else 0
                for item in items
            ]
        return flt_flags, flt_neworder


# --------------------------------------------------------------------------
# Main SDK Operators
# --------------------------------------------------------------------------

class SDK_OT_load_driver(Operator):
    bl_idname = "sdk.load_driver"
    bl_label = "Load Driver"
    bl_description = "Use the active object (or active bone in Pose Mode) as the driver"
    bl_options = {"REGISTER", "UNDO"}

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
        invalidate_keyed_cache()
        return {"FINISHED"}


class SDK_OT_capture_driver_rest(Operator):
    bl_idname = "sdk.capture_driver_rest"
    bl_label = "Capture Driver Rest"
    bl_description = "Use the driver's current value as its rest value"
    bl_options = {"REGISTER", "UNDO"}

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
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        try:
            set_by_path(drv_id, d_var_path, -1, s.driver_rest)
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
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        try:
            cur = float(drv_id.path_resolve(d_var_path))
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver value")
            return {"CANCELLED"}

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

        set_by_path(drv_id, d_var_path, -1, target)
        context.view_layer.update()
        self.report({"INFO"}, "Jumped to driver key: %.4f" % target)
        return {"FINISHED"}


class SDK_OT_channel_preset(Operator):
    bl_idname = "sdk.channel_preset"
    bl_label = "Channel Preset"
    bl_description = "Quick toggle channel selection presets"
    bl_options = {"REGISTER", "UNDO"}

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
        invalidate_keyed_cache()
        return {"FINISHED"}


class SDK_OT_load_driven(Operator):
    bl_idname = "sdk.load_driven"
    bl_label = "Load Driven"
    bl_description = "Use selected objects (or selected bones in Pose Mode) as driven targets"
    bl_options = {"REGISTER", "UNDO"}

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
        invalidate_keyed_cache()
        return {"FINISHED"}


class SDK_OT_load_shape_keys(Operator):
    bl_idname = "sdk.load_shape_keys"
    bl_label = "Load Mesh Shape Keys"
    bl_description = "Add all shape keys of the active mesh to custom driven channels"
    bl_options = {"REGISTER", "UNDO"}

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

        # Ensure the mesh is present in driven_items so keying actually runs
        already = any(it.obj == obj and not it.bone for it in s.driven_items)
        if not already:
            item = s.driven_items.add()
            item.obj = obj
            item.bone = ""
            capture(item)

        invalidate_keyed_cache()
        self.report({"INFO"}, "Added %d shape key(s) to Custom driven channels" % len(keys))
        return {"FINISHED"}


class SDK_OT_remove_driven(Operator):
    bl_idname = "sdk.remove_driven"
    bl_label = "Remove Driven"
    bl_description = "Remove the highlighted entry from the driven list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if 0 <= s.driven_index < len(s.driven_items):
            s.driven_items.remove(s.driven_index)
            s.driven_index = max(0, s.driven_index - 1)
        invalidate_keyed_cache()
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


def _flip_bone_name(name):
    patterns = [
        (r"\.L$", ".R"), (r"\.R$", ".L"),
        (r"_L$", "_R"), (r"_R$", "_L"),
        (r"\.left$", ".right"), (r"\.right$", ".left"),
        (r"_left$", "_right"), (r"_right$", "_left"),
        (r"\.Left$", ".Right"), (r"\.Right$", ".Left"),
        (r"_Left$", "_Right"), (r"_Right$", "_Left"),
    ]
    for pat, rep in patterns:
        new_n, c = re.subn(pat, rep, name, flags=re.IGNORECASE)
        if c > 0:
            return new_n
    return ""


def _copy_fcurve_keys(src_fc, dst_fc, flip_sign=1.0):
    while len(dst_fc.keyframe_points):
        dst_fc.keyframe_points.remove(dst_fc.keyframe_points[0], fast=True)
    for kp in src_fc.keyframe_points:
        key_curve(dst_fc, kp.co.x, kp.co.y * flip_sign, kp.interpolation, src_fc.extrapolation)


class SDK_OT_mirror_keys(Operator):
    bl_idname = "sdk.mirror_keys"
    bl_label = "Mirror / Flip to Symmetrical Bones"
    bl_description = "Copy and mirror SDK drivers and curves to counterpart bones (e.g. .L <-> .R)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        mirrored = 0
        constraints_mirrored = 0
        mode = s.mode

        for item in s.driven_items:
            if not item.bone or not item.obj:
                continue
            opp_bone = _flip_bone_name(item.bone)
            if not opp_bone or opp_bone not in item.obj.pose.bones:
                continue

            # Build a temporary driven item for the opposite bone
            class _Opp:
                pass
            opp_item = _Opp()
            opp_item.obj = item.obj
            opp_item.bone = opp_bone
            # Capture rest for the opposite bone so additive math stays valid
            pb = item.obj.pose.bones[opp_bone]
            opp_item.rest_loc = pb.location.copy()
            opp_item.rest_euler = pb.rotation_euler.copy()
            opp_item.rest_quat = pb.rotation_quaternion.copy()
            opp_item.rest_scale = pb.scale.copy()

            if mode == "DIRECT":
                for kind, axis in transform_specs(s):
                    src_path = full_path(item.bone, DIRECT_ATTR[kind])
                    dst_path = full_path(opp_bone, DIRECT_ATTR[kind])
                    src_fc = find_driver_fcurve(item.obj, src_path, axis)
                    if src_fc is None or not is_sdk_fcurve(src_fc):
                        continue
                    flip_sign = -1.0 if (axis == 0 and kind in ("loc", "rot")) else 1.0
                    dst_fc, msg = ensure_driver(item.obj, dst_path, axis, drv_id, d_var_path, not s.live_preview)
                    if dst_fc:
                        _copy_fcurve_keys(src_fc, dst_fc, flip_sign)
                        mirrored += 1

            elif mode == "ADDITIVE":
                for kind, axis in transform_specs(s):
                    # Ensure opposite constraint exists
                    try:
                        ensure_constraint(opp_item, kind, scope_key, not s.live_preview)
                        constraints_mirrored += 1
                    except Exception:  # noqa: BLE001
                        continue
                    src_targets = additive_targets(item, kind, axis, scope_key)
                    dst_targets = additive_targets(opp_item, kind, axis, scope_key)
                    flip_sign = -1.0 if (axis == 0 and kind in ("loc", "rot")) else 1.0
                    for (src_path, src_idx), (dst_path, dst_idx) in zip(src_targets, dst_targets):
                        src_fc = find_driver_fcurve(item.obj, src_path, src_idx)
                        if src_fc is None or not is_sdk_fcurve(src_fc):
                            continue
                        dst_fc, msg = ensure_driver(item.obj, dst_path, dst_idx, drv_id, d_var_path, not s.live_preview)
                        if dst_fc:
                            _copy_fcurve_keys(src_fc, dst_fc, flip_sign)
                            mirrored += 1

            elif mode == "ACTION":
                act = bpy.data.actions.get("%s%s" % (SDK_ACTION_PREFIX, scope_key))
                if act is None:
                    continue
                # Ensure opposite Action constraint
                con, msg = ensure_action_constraint(s, opp_item, act, scope_key, drv_id, drv_bone, d_var_path)
                if con:
                    constraints_mirrored += 1
                for kind, axis in transform_specs(s):
                    src_path = full_path(item.bone, DIRECT_ATTR[kind])
                    dst_path = full_path(opp_bone, DIRECT_ATTR[kind])
                    find_idx = axis
                    src_fc = act.fcurves.find(src_path, index=find_idx)
                    if src_fc is None:
                        continue
                    dst_fc = act.fcurves.find(dst_path, index=find_idx)
                    if dst_fc is None:
                        try:
                            dst_fc = act.fcurves.new(dst_path, index=find_idx)
                        except Exception:  # noqa: BLE001
                            continue
                    flip_sign = -1.0 if (axis == 0 and kind in ("loc", "rot")) else 1.0
                    _copy_fcurve_keys(src_fc, dst_fc, flip_sign)
                    mirrored += 1
                # Quaternions / custom channels inside the action
                for i in quat_specs(s):
                    src_path = full_path(item.bone, "rotation_quaternion")
                    dst_path = full_path(opp_bone, "rotation_quaternion")
                    src_fc = act.fcurves.find(src_path, index=i)
                    if src_fc is None:
                        continue
                    dst_fc = act.fcurves.find(dst_path, index=i)
                    if dst_fc is None:
                        try:
                            dst_fc = act.fcurves.new(dst_path, index=i)
                        except Exception:  # noqa: BLE001
                            continue
                    # Quaternion mirroring is non-trivial; copy as-is (W/Y/Z often keep sign)
                    flip = -1.0 if i in (1, 2) else 1.0  # rough X-axis mirror heuristic
                    _copy_fcurve_keys(src_fc, dst_fc, flip)
                    mirrored += 1
                if act:
                    update_action_constraint_ranges(act, s, scope_key)

        invalidate_keyed_cache()
        context.view_layer.update()
        self.report(
            {"INFO"},
            "Mirrored %d curve(s), %d constraint(s) across symmetry" % (mirrored, constraints_mirrored),
        )
        return {"FINISHED"}


def _check_mode_conflicts(s, item, path, index, mode):
    """Return a warning string if a non-SDK or cross-mode driver already exists."""
    obj = item.obj
    if obj is None:
        return ""
    target_id, rel_path = resolve_id_and_path(obj, path)
    fc = find_driver_fcurve(target_id, rel_path, index)
    if fc is None:
        return ""
    if not is_sdk_fcurve(fc):
        return "'%s' has a non-SDK driver" % var_path(rel_path, index)
    # Detect possible cross-mode conflict: Additive targets vs Direct on same channel
    if mode == "DIRECT" and ".constraints[" in path:
        return ""  # Direct shouldn't target constraint paths
    if mode == "ADDITIVE" and ".constraints[" not in path:
        # Direct-style path while in Additive — may be intentional for custom
        pass
    return ""


class SDK_OT_key(Operator):
    bl_idname = "sdk.key"
    bl_label = "Key"
    bl_description = "Store driven channels against current driver value (creates drivers/curves if needed)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        try:
            val = drv_id.path_resolve(d_var_path)
            x = float(val)
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
        action = get_action_for_driver(s, scope_key) if mode == "ACTION" else None

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
                path_check = full_path(item.bone, DIRECT_ATTR[kind])
                warn = _check_mode_conflicts(s, item, path_check, axis, mode)
                if warn:
                    problems.append(warn)
                if mode == "ADDITIVE":
                    msg = safe_call(key_additive, label, s, item, kind, axis,
                                    drv_id, d_var_path, idx, d_var_path, scope_key, x)
                elif mode == "DIRECT":
                    msg = safe_call(key_direct, label, s, item, DIRECT_ATTR[kind], axis,
                                    drv_id, d_var_path, idx, d_var_path, x)
                else:  # ACTION
                    msg = safe_call(key_action_mode, label, s, item, DIRECT_ATTR[kind], axis,
                                    action, scope_key, drv_id, drv_bone, x, d_var_path)
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
                                        drv_id, d_var_path, idx, d_var_path, x)
                    else:  # ACTION
                        msg = safe_call(key_action_mode, label, s, item, "rotation_quaternion", i,
                                        action, scope_key, drv_id, drv_bone, x, d_var_path)
                    if msg:
                        problems.append(msg)
                    else:
                        keyed += 1

            for rel, c_idx in cspecs:
                label = "%s %s" % (item.bone or obj.name, rel)
                # Custom paths always use Direct-style keying (including under Action mode
                # for shape keys etc.); Action transform channels stay on transforms.
                msg = safe_call(key_direct, label, s, item, rel, c_idx, drv_id, d_var_path, idx, d_var_path, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1

            if (mode == "ADDITIVE" or mode == "ACTION") and s.auto_reset and done:
                reset_base(item, done)

        if mode == "ACTION" and action:
            update_action_constraint_ranges(action, s, scope_key)

        invalidate_keyed_cache()
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
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        try:
            val = drv_id.path_resolve(d_var_path)
            x = float(val)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver property")
            return {"CANCELLED"}

        removed = 0
        if s.mode == "ACTION":
            act = bpy.data.actions.get("%s%s" % (SDK_ACTION_PREFIX, scope_key))
            if act:
                frame = x * 100.0
                for fc in list(act.fcurves):
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - frame) < KEY_EPS * 100.0:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                update_action_constraint_ranges(act, s, scope_key)
        else:
            for item in s.driven_items:
                obj = item.obj
                if obj is None:
                    continue
                for path, p_idx in all_targets(s, item, scope_key):
                    target_id, rel_path = resolve_id_and_path(obj, path)
                    fc = find_driver_fcurve(target_id, rel_path, p_idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - x) < KEY_EPS:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                            break
        invalidate_keyed_cache()
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
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        removed = 0

        if s.mode == "ACTION":
            act = bpy.data.actions.get("%s%s" % (SDK_ACTION_PREFIX, scope_key))
            con_name = action_constraint_name_for(scope_key)
            for item in s.driven_items:
                owner = owner_of(item)
                if owner and con_name in owner.constraints:
                    con = owner.constraints[con_name]
                    owner.constraints.remove(con)
                    removed += 1
            if act and act.users <= 1:
                bpy.data.actions.remove(act)
        else:
            for item in s.driven_items:
                obj = item.obj
                if obj is None:
                    continue
                for path, p_idx in all_targets(s, item, scope_key):
                    target_id, rel_path = resolve_id_and_path(obj, path)
                    fc = find_driver_fcurve(target_id, rel_path, p_idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    try:
                        if p_idx >= 0:
                            target_id.driver_remove(rel_path, p_idx)
                        else:
                            target_id.driver_remove(rel_path)
                    except Exception:  # noqa: BLE001
                        continue
                    removed += 1
                    if ".constraints[" in path:
                        try:
                            set_by_path(obj, path, p_idx, identity_for(path))
                        except Exception:  # noqa: BLE001
                            pass

                owner = owner_of(item)
                if owner and obj.animation_data:
                    drivers = list(obj.animation_data.drivers)
                    for con in list(owner.constraints):
                        if con.name.startswith("SDK "):
                            prefix = full_path(item.bone, 'constraints["%s"]' % con.name)
                            if not any(fc.data_path.startswith(prefix) for fc in drivers):
                                owner.constraints.remove(con)

        cleanup_dummy_target()
        invalidate_keyed_cache()
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

        # ---- Configurations Manager ---------------------------------------
        box = layout.box()
        box.label(text="Configurations", icon="PRESET")
        row = box.row(align=True)
        row.template_list("SDK_UL_configs", "", s, "configs", s, "config_index", rows=3)
        col = row.column(align=True)
        col.operator("sdk.config_add", text="", icon="ADD")
        col.operator("sdk.config_remove", text="", icon="REMOVE")
        col.operator("sdk.config_load", text="", icon="IMPORT")

        row = box.row(align=True)
        row.prop(s, "config_layer_tag", text="Layer Tag")
        row.operator("sdk.config_load", text="Load / Activate", icon="CHECKMARK")

        # ---- Driver -------------------------------------------------------
        box = layout.box()
        box.label(text="Driver", icon="DRIVER")
        box.operator("sdk.load_driver", icon="EYEDROPPER")
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id:
            name = drv_id.name + (" : " + drv_bone if drv_bone else "")
            box.label(text=name, icon="BONE_DATA" if drv_bone else "OBJECT_DATA")
            box.prop(s, "driver_channel", text="")
            if s.driver_channel == "CUSTOM":
                box.prop(s, "driver_custom")
            box.prop(s, "driver_scrub", text="Scrub Value", slider=True)
            row = box.row(align=True)
            row.prop(s, "driver_rest", text="Rest")
            row.operator("sdk.capture_driver_rest", text="", icon="PINNED")
            row.operator("sdk.driver_to_rest", text="", icon="LOOP_BACK")

            # Keyframe navigation helpers — use cache; only recompute when needed
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
        if s.mode == "ADDITIVE" or s.mode == "ACTION":
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
    SDK_ConfigItem,
    SDK_DrivenItem,
    SDK_Settings,
    SDK_OT_config_add,
    SDK_OT_config_load,
    SDK_OT_config_remove,
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
    SDK_UL_configs,
    SDK_UL_driven,
    SDK_PT_main,
    SDK_PT_options,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)


def unregister():
    cleanup_dummy_target()
    del bpy.types.Scene.sdk_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
