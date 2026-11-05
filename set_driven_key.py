# ==========================================================================
# Set Driven Key 1.5.0 - change notes (vs 1.4.0)
#
#  * set_by_path(): token-based resolver; array elements (location[0]),
#    ID props (["p"], pose.bones["B"]["p"]) and shape-key paths all work.
#    Scrub / Prev-Next Key / Driver to Rest use the same read/write helpers.
#  * find_driver_fcurve(): scalar drivers live at array_index 0 in Blender
#    (drivers.find(path, index=-1) never matches), so -1 is translated to 0
#    but an *array* property without index no longer matches element 0.
#  * Mirror: Direct, Additive (scoped Transformation constraints) and Action
#    Constraint modes; signs come from the bones' rest matrices (falls back
#    to Blender's X-mirror convention); .L/.R, _L/_R, .left/.right, prefixes.
#  * Load Mesh Shape Keys also adds the mesh to the driven list.
#  * Action mode with non-transform drivers (custom props, quaternion, other
#    IDs) uses the constraint's Evaluation Time, driven by an SDK f-curve.
#    Mapping (all Action modes): driver value v -> action frame v * 100.
#  * add_rest_key() never skipped on empty / single-pending-key curves;
#    custom channels remember their rest value (SDK_DrivenItem.rest_custom).
#  * SDK_Constraint_Target is parked in a hidden collection and removed when
#    unused (Delete Curves, unregister).
#  * Live Preview mutes/unmutes drivers (incl. shape-key IDs) and SDK
#    constraints (Additive + Action) through one shared function.
#  * Configurations store pointers + name/data/library fallbacks; JSON
#    Export / Import; clear warnings for missing items.
#  * Mode-conflict detection when keying (Direct vs Additive vs Action,
#    foreign drivers, driver re-binding).
#  * Keyed-value list in the panel is cached (invalidated by operators,
#    undo/redo, file load); narrower exception handling; UNDO on all
#    data-changing operators.
# ==========================================================================

bl_info = {
    "name": "Set Driven Key",
    "author": "OceanMan, CloudKing, Apixelitonomo, U.F.Olive",
    "version": (1, 5, 0),
    "blender": (3, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Driven Key",
    "description": "Professional Set Driven Key for Blender with Configuration Manager, Multi-Axis Stacking, Additive, Direct, and Action Constraint modes",
    "category": "Animation",
}

import json
import math
import re
import traceback
import zlib
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
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
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Euler, Matrix, Quaternion

# Name of the driver variable the add-on creates.
SDK_VAR = "sdk_driver"

# Constraint naming prefixes
SDK_CON_BASE = {"loc": "SDK Location", "rot": "SDK Rotation", "scale": "SDK Scale"}
CON_SUFFIX = {"loc": "", "rot": "_rot", "scale": "_scale"}
DIRECT_ATTR = {"loc": "location", "rot": "rotation_euler", "scale": "scale"}
_ATTR_KIND = {v: k for k, v in DIRECT_ATTR.items()}
_CHANNEL_ATTRS = ("location", "rotation_euler", "rotation_quaternion", "scale")

# Action constraint naming
SDK_ACTION_PREFIX = "SDK_Action_"
SDK_ACTION_CON_PREFIX = "SDK Action ["

# Action Constraint mapping: driver value v  ->  action frame v * ACTION_FRAMES_PER_UNIT
ACTION_FRAMES_PER_UNIT = 100.0

KEY_EPS = 0.01  # Blender treats keys closer than this (on the X axis) as the same key

DUMMY_TARGET_NAME = "SDK_Constraint_Target"
DUMMY_COLLECTION_NAME = "SDK_Internal"

MAX_ID_NAME = 63  # bytes available for constraint / action names

# Errors raised by RNA when a path / property / value is invalid.
_RNA_ERRORS = (AttributeError, KeyError, IndexError, TypeError, ValueError, RuntimeError, ReferenceError)
_READ_ERRORS = _RNA_ERRORS + (LookupError,)

# Non-fatal messages collected while an operator runs.
_WARNINGS = []


def _warn(msg):
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)


def _report_problems(op, problems):
    uniq = sorted(set(problems))
    if not uniq:
        return
    for p in uniq:
        print("[SDK] " + p)
    text = "; ".join(uniq[:3])
    if len(uniq) > 3:
        text += " (+%d more, see the system console)" % (len(uniq) - 3)
    op.report({"WARNING"}, text)


# --------------------------------------------------------------------------
# Path / value helpers
# --------------------------------------------------------------------------

_CUSTOM_RE = re.compile(r"^(.+)\[(\d+)\]$")
_BPY_DATA_RE = re.compile(r"^bpy\.data\.([a-zA-Z0-9_]+)\[[\"\']([^\"\']+)[\"\']\]\.(.+)$")
_BONE_SUBPATH_RE = re.compile(r"^pose\.bones\[[\"\']([^\"\']+)[\"\']\]\.(.+)$")

# One token of a data path: .attr | ["key"] | ['key'] | [int]
_PATH_TOKEN_RE = re.compile(
    r'\.?(?P<attr>[A-Za-z_][A-Za-z0-9_]*)'
    r'|\[(?:"(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\'|(?P<idx>-?\d+))\]'
)


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


def con_data_path(con_name):
    """constraints["<name>"] with the name escaped."""
    return 'constraints["%s"]' % bpy.utils.escape_identifier(con_name)


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
    value = id_block.path_resolve(rel_path)
    if index >= 0:
        value = value[index]
    return float(value)


def _split_path(path):
    """Split a data path into [(kind, value, start)]; kind is 'attr' | 'key' | 'idx'."""
    tokens = []
    pos = 0
    while pos < len(path):
        m = _PATH_TOKEN_RE.match(path, pos)
        if m is None:
            raise ValueError("Unsupported data path '%s'" % path)
        if m.group("attr") is not None:
            tokens.append(("attr", m.group("attr"), pos))
        elif m.group("idx") is not None:
            tokens.append(("idx", int(m.group("idx")), pos))
        else:
            raw = m.group("dq") if m.group("dq") is not None else m.group("sq")
            tokens.append(("key", raw, pos))
        pos = m.end()
    return tokens


def set_by_path(obj, path, index, value):
    """Assign `value` to the property at `path`.

    Handles, relative to `obj`:
      location / location[0] / pose.bones["B"].location[0]   (attribute or array element)
      ["prop"] / ["prop"][1] / pose.bones["B"]["prop"]       (ID properties)
      data.shape_keys.key_blocks["N"].value                  (shape-key paths)
    A trailing [n] in the path wins over `index`; otherwise `index >= 0` selects the element.
    """
    id_block, rel_path = resolve_id_and_path(obj, path)
    tokens = _split_path(rel_path)
    if not tokens:
        raise ValueError("Empty data path")

    elem = None
    if tokens[-1][0] == "idx" and len(tokens) > 1:
        elem = tokens[-1][1]
        tokens = tokens[:-1]
    elif index >= 0:
        elem = index

    kind, name, start = tokens[-1]
    owner = id_block.path_resolve(rel_path[:start]) if start > 0 else id_block

    if kind == "attr":
        if elem is None:
            setattr(owner, name, value)
        else:
            getattr(owner, name)[elem] = value
    elif kind == "key":
        key = bpy.utils.unescape_identifier(name)
        if elem is None:
            owner[key] = value
        else:
            owner[key][elem] = value
    else:
        raise ValueError("Path '%s' ends in an index with nothing to index" % path)


def read_driver_value(s):
    """Current value of the driver property (raises on failure)."""
    drv_id, _b, _a, _i, d_var_path, _k = resolve_driver_target(s)
    if drv_id is None:
        raise LookupError("No driver loaded")
    return float(drv_id.path_resolve(d_var_path))


def write_driver_value(s, value):
    """Write the driver property (raises on failure)."""
    drv_id, _b, _a, _i, d_var_path, _k = resolve_driver_target(s)
    if drv_id is None:
        raise LookupError("No driver loaded")
    set_by_path(drv_id, d_var_path, -1, value)
    try:
        drv_id.update_tag()
    except _RNA_ERRORS:
        pass


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


def enabled_channels(s):
    """{(attr, axis)} for the enabled Location / Rotation / Scale / Quaternion toggles."""
    out = {(DIRECT_ATTR[k], a) for k, a in transform_specs(s)}
    out.update(("rotation_quaternion", i) for i in quat_specs(s))
    return out


def capture_driver_rest(s):
    """Remember the driver's current value as its rest value."""
    try:
        s.driver_rest = read_driver_value(s)
    except _READ_ERRORS:
        pass


# --------------------------------------------------------------------------
# Dummy constraint target (hidden Empty) and its cleanup
# --------------------------------------------------------------------------

def _dummy_collection(scene):
    coll = bpy.data.collections.get(DUMMY_COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(DUMMY_COLLECTION_NAME)
        coll.hide_render = True
        coll.hide_viewport = True
    if scene is not None and scene.collection is not None:
        if coll.name not in scene.collection.children:
            scene.collection.children.link(coll)
    return coll


def get_or_create_dummy_target():
    """Ensure a dummy non-evaluating Empty object exists to satisfy Transformation / Action constraints."""
    scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        empty = bpy.data.objects.new(DUMMY_TARGET_NAME, None)
        empty.empty_display_size = 0.001
        empty.hide_render = True
        empty.hide_viewport = True
    if not empty.users_collection:
        _dummy_collection(scene).objects.link(empty)
    return empty


def _constraint_owners(obj):
    yield obj
    if obj.pose:
        for pb in obj.pose.bones:
            yield pb


def dummy_target_in_use():
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        return False
    for ob in bpy.data.objects:
        if ob == empty:
            continue
        for owner in _constraint_owners(ob):
            for con in owner.constraints:
                if getattr(con, "target", None) == empty:
                    return True
    return False


def cleanup_dummy_target():
    """Remove SDK_Constraint_Target (and its hidden collection) when no constraint references it."""
    try:
        empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
        if empty is None or dummy_target_in_use():
            return False
        bpy.data.objects.remove(empty, do_unlink=True)
        coll = bpy.data.collections.get(DUMMY_COLLECTION_NAME)
        if coll is not None and not coll.objects and not coll.children:
            bpy.data.collections.remove(coll)
        return True
    except _RNA_ERRORS:
        traceback.print_exc()
        return False


# --------------------------------------------------------------------------
# Driver / curve helpers (High performance O(1) lookups)
# --------------------------------------------------------------------------

def _is_array_property(target_id, path):
    """True if `path` resolves to an array (vector, list, ID-prop array)."""
    try:
        value = target_id.path_resolve(path)
    except _RNA_ERRORS:
        return False
    return hasattr(value, "__len__") and not isinstance(value, str)


def find_driver_fcurve(target_id, path, index):
    """Fast driver lookup using drivers.find(path, index).

    Blender stores the driver of a scalar property at array_index 0 and
    BKE_fcurve_find compares indices exactly, so index == -1 ("not an array
    element") is looked up as 0 - but only if the property really is a scalar,
    otherwise `location` would wrongly match the driver of `location[0]`.
    """
    ad = target_id.animation_data
    if ad is None:
        return None
    want = index if index >= 0 else 0
    try:
        fc = ad.drivers.find(path, index=want)
    except TypeError:
        fc = next((f for f in ad.drivers if f.data_path == path and f.array_index == want), None)
    if fc is not None and index < 0 and _is_array_property(target_id, path):
        return None
    return fc


def is_sdk_fcurve(fc):
    return any(v.name == SDK_VAR for v in fc.driver.variables)


def driver_matches(fc, drv_obj, d_var_path):
    """True if the SDK f-curve is driven by exactly this driver property."""
    var = next((v for v in fc.driver.variables if v.name == SDK_VAR), None)
    if var is None:
        return False
    tgt = var.targets[0]
    return tgt.id == drv_obj and tgt.data_path == d_var_path


def _id_type_name(drv_obj):
    t = getattr(drv_obj, "id_type", None)
    if t:
        return t
    type_str = getattr(getattr(drv_obj, "rna_type", None), "name", "Object").upper().replace(" ", "")
    if type_str in ("KEY", "OBJECT", "ARMATURE", "SCENE", "MATERIAL", "NODETREE"):
        return type_str
    return "OBJECT"


def ensure_driver(obj, path, index, drv_obj, drv_data_path, mute):
    """Return (fcurve, message). fcurve is None if the channel can't be used."""
    target_id, rel_path = resolve_id_and_path(obj, path)
    fc = find_driver_fcurve(target_id, rel_path, index)
    if fc is None:
        if index < 0 and _is_array_property(target_id, rel_path):
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        ad = target_id.animation_data
        before = {f.array_index for f in ad.drivers if f.data_path == rel_path} if ad else set()
        try:
            fc = target_id.driver_add(rel_path, index) if index >= 0 else target_id.driver_add(rel_path)
        except _RNA_ERRORS as e:
            return None, "Cannot drive '%s': %s" % (rel_path, e)
        if isinstance(fc, (list, tuple)):
            # Defensive: only remove the drivers we just created, never pre-existing ones.
            for f in fc:
                if f.array_index not in before:
                    try:
                        target_id.driver_remove(rel_path, f.array_index)
                    except _RNA_ERRORS:
                        pass
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        fc.mute = mute
    elif not is_sdk_fcurve(fc):
        return None, ("'%s' already has a driver that wasn't made by this add-on "
                      "(remove or rename it first)" % var_path(rel_path, index))

    drv = fc.driver
    var = next((v for v in drv.variables if v.name == SDK_VAR), None)

    same = (
        var is not None
        and var.targets[0].id == drv_obj
        and var.targets[0].data_path == drv_data_path
    )
    if not same:
        if var is not None and len(fc.keyframe_points):
            _warn("'%s' was re-bound to a different driver; its previous keys were discarded"
                  % var_path(rel_path, index))
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[0], fast=True)
        for v in list(drv.variables):
            drv.variables.remove(v)
        var = drv.variables.new()
        var.name = SDK_VAR
        var.type = "SINGLE_PROP"
        t = var.targets[0]
        t.id_type = _id_type_name(drv_obj)
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


def add_rest_key(s, fc, x, rest_y, x_scale=1.0):
    """Key the rest state (driver at its rest value) before / together with the posed key.

    Inserted when the curve is empty or holds only the key about to be written.
    `x` is in driver units; `x_scale` converts to the curve's X unit (frames for Actions).
    Returns True if a rest key was written.
    """
    if rest_y is None or not s.auto_rest_key:
        return False
    if abs(x - s.driver_rest) < KEY_EPS:
        return False  # the posed key IS the rest key
    kps = fc.keyframe_points
    n = len(kps)
    only_pending = n == 0 or (n == 1 and abs(kps[0].co.x - x * x_scale) < KEY_EPS)
    if not only_pending:
        return False
    key_curve(fc, s.driver_rest * x_scale, rest_y, s.interpolation, s.extrapolation)
    return True


def copy_curve_flipped(src, dst, sign):
    """Replace all keys of `dst` with those of `src`, values multiplied by `sign`."""
    kps = dst.keyframe_points
    while len(kps):
        kps.remove(kps[0], fast=True)
    auto = ("AUTO", "AUTO_CLAMPED")
    for kp in src.keyframe_points:
        key_curve(dst, kp.co.x, kp.co.y * sign, kp.interpolation, src.extrapolation)
        new = next((k for k in dst.keyframe_points if abs(k.co.x - kp.co.x) < KEY_EPS), None)
        if new is None:
            continue
        new.handle_left_type = kp.handle_left_type
        new.handle_right_type = kp.handle_right_type
        if kp.handle_left_type not in auto:
            new.handle_left = (kp.handle_left.x, kp.handle_left.y * sign)
        if kp.handle_right_type not in auto:
            new.handle_right = (kp.handle_right.x, kp.handle_right.y * sign)
    dst.update()


def safe_call(fn, label, *args):
    """Run one key operation. A failure becomes a message instead of aborting everything.

    The broad except is intentional: this is the per-channel error boundary.
    """
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return "%s failed: %s" % (label, e)


def _fit_scope(scope_key, overhead):
    """Shorten `scope_key` so that overhead + scope fits Blender's 63-byte name limit."""
    room = MAX_ID_NAME - overhead
    if len(scope_key.encode("utf-8")) <= room:
        return scope_key
    tag = "~%08x" % (zlib.crc32(scope_key.encode("utf-8")) & 0xFFFFFFFF)
    head = scope_key
    while len(head.encode("utf-8")) > room - len(tag):
        head = head[:-1]
    return head + tag


# --------------------------------------------------------------------------
# SDK constraint predicates, Live Preview muting
# --------------------------------------------------------------------------

_SDK_TRANSFORM_PREFIXES = tuple(v + " [" for v in SDK_CON_BASE.values())


def is_sdk_transform_constraint(con):
    return con.type == "TRANSFORM" and con.name.startswith(_SDK_TRANSFORM_PREFIXES) and con.name.endswith("]")


def is_sdk_action_constraint(con):
    return con.type == "ACTION" and con.name.startswith(SDK_ACTION_CON_PREFIX) and con.name.endswith("]")


def is_sdk_constraint(con):
    return is_sdk_transform_constraint(con) or is_sdk_action_constraint(con)


def _sdk_id_blocks(obj):
    """ID blocks of an object that can own SDK drivers (object + its shape-key datablock)."""
    blocks = [obj]
    keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if keys is not None:
        blocks.append(keys)
    return blocks


def set_sdk_muted(objs, muted):
    """Mute / unmute every SDK driver and SDK constraint (Additive + Action) on `objs`."""
    seen = set()
    for obj in objs:
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        for block in _sdk_id_blocks(obj):
            ad = block.animation_data
            if ad:
                for fc in ad.drivers:
                    if is_sdk_fcurve(fc):
                        fc.mute = muted
        for owner in _constraint_owners(obj):
            for con in owner.constraints:
                if is_sdk_constraint(con):
                    con.mute = muted


# --------------------------------------------------------------------------
# Rest values
# --------------------------------------------------------------------------

def capture(item, s=None):
    """Snapshot the base channels (the 'rest' that additive offsets are measured from)."""
    t = owner_of(item)
    if t is None:
        return
    item.rest_loc = t.location
    item.rest_euler = t.rotation_euler
    item.rest_quat = t.rotation_quaternion
    item.rest_scale = t.scale
    if s is not None:
        capture_custom_rest(item, s)


def capture_custom_rest(item, s, only_missing=False):
    """Remember the current value of the custom driven channels (shape keys, ID props)."""
    obj = item.obj
    if obj is None:
        return
    try:
        store = json.loads(item.rest_custom or "{}")
    except ValueError:
        store = {}
    for rel, idx in custom_specs(s):
        key = var_path(rel, idx)
        if only_missing and key in store:
            continue
        try:
            store[key] = get_value(obj, full_path(item.bone, rel), idx)
        except _RNA_ERRORS:
            continue
    item.rest_custom = json.dumps(store)


def rest_value_for(item, attr, idx):
    """Rest value of a driven channel, or None if unknown (then no rest key is written)."""
    if idx >= 0:
        vec = {
            "location": item.rest_loc,
            "rotation_euler": item.rest_euler,
            "rotation_quaternion": item.rest_quat,
            "scale": item.rest_scale,
        }.get(attr)
        if vec is not None:
            return vec[idx]
    try:
        store = json.loads(item.rest_custom or "{}")
    except ValueError:
        store = {}
    key = var_path(attr, idx)
    if key in store:
        return float(store[key])
    if attr.endswith(".value") and "key_blocks[" in attr:
        return 0.0  # shape keys rest at 0
    return None


# --------------------------------------------------------------------------
# Additive mode helpers (Multi-axis scoped constraints)
# --------------------------------------------------------------------------

def constraint_name_for(kind, scope_key):
    """Unique constraint name per configuration and driver axis/channel."""
    base = SDK_CON_BASE[kind]
    return "%s [%s]" % (base, _fit_scope(scope_key, len(base) + 3))


def additive_targets(item, kind, axis, scope_key):
    """(path, index) pairs the SDK drives for one transform channel in additive mode."""
    con_name = constraint_name_for(kind, scope_key)
    a = "xyz"[axis]
    rel = con_data_path(con_name) + "."
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


# --------------------------------------------------------------------------
# Mode conflict detection
# --------------------------------------------------------------------------

def mode_conflict(item, mode, attr, axis, scope_key=""):
    """Return '' or a message if the channel is already controlled by an SDK setup of another mode.

    Same-mode stacking is by design: Additive layers and Action constraints of other
    scopes stack, so they are not conflicts for their own mode.
    """
    obj = item.obj
    owner = owner_of(item)
    if obj is None or owner is None or attr not in _CHANNEL_ATTRS or axis < 0:
        return ""
    label = "%s %s[%d]" % (item.bone or obj.name, attr, axis)
    path = full_path(item.bone, attr)

    if mode != "DIRECT":
        fc = find_driver_fcurve(obj, path, axis)
        if fc is not None and is_sdk_fcurve(fc):
            return "%s is already driven in Direct mode (use Delete Curves first)" % label

    if mode != "ADDITIVE" and attr in _ATTR_KIND:
        kind = _ATTR_KIND[attr]
        prefix = SDK_CON_BASE[kind] + " ["
        for con in owner.constraints:
            if not is_sdk_transform_constraint(con) or not con.name.startswith(prefix):
                continue
            for p, i in additive_targets(item, kind, axis, con.name[len(prefix):-1]):
                fc = find_driver_fcurve(obj, p, i)
                if fc is not None and is_sdk_fcurve(fc) and len(fc.keyframe_points):
                    return "%s already has the Additive SDK layer '%s' (use Delete Curves first)" % (label, con.name)

    if mode != "ACTION":
        for con in owner.constraints:
            if not is_sdk_action_constraint(con) or con.action is None:
                continue
            try:
                fcs = action_fcurves(con.action)
            except RuntimeError:
                break
            if fcs.find(path, index=axis) is not None:
                return "%s already has the Action SDK constraint '%s' (use Delete Curves first)" % (label, con.name)
    return ""


def key_additive(s, item, kind, axis, drv_obj, d_path, d_idx, d_var_path, scope_key, x):
    """Key one transform channel as an additive offset. Supports multiple driver axes."""
    obj = item.obj
    owner = owner_of(item)
    if item.bone and obj == drv_obj and item.bone == s.driver_bone:
        return "Bone '%s' can't drive itself" % item.bone

    conflict = mode_conflict(item, "ADDITIVE", DIRECT_ATTR[kind], axis, scope_key)
    if conflict:
        return conflict

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
            except _RNA_ERRORS:
                existing = ident
    value = existing * base_off if kind == "scale" else existing + base_off

    try:
        ensure_constraint(item, kind, scope_key, not s.live_preview)
    except _RNA_ERRORS as e:
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
    if obj == drv_obj and var_path(path, idx) == d_var_path:
        return "'%s' can't drive itself" % var_path(path, idx)
    conflict = mode_conflict(item, "DIRECT", attr, idx)
    if conflict:
        return conflict
    try:
        y = get_value(obj, path, idx)
    except _RNA_ERRORS:
        return "Can't read '%s'" % var_path(path, idx)
    fc, msg = ensure_driver(obj, path, idx, drv_obj, d_var_path, not s.live_preview)
    if fc is None:
        return msg

    add_rest_key(s, fc, x, rest_value_for(item, attr, idx))
    key_curve(fc, x, y, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Action Constraint Mode Helpers (Multi-axis & dynamic stacking support)
#
# Mapping (documented contract):
#   * Driver value v  ->  action frame  v * ACTION_FRAMES_PER_UNIT (100).
#   * Driver = Location / Rotation / Scale [0-2] of an Object or pose bone:
#       the constraint reads that transform channel (transform_channel) and
#       min/max = keyed driver range, frame_start/end = floor/ceil of the keyed
#       frames, so  frame = frame_start + (v - min)/(max - min) * (end - start) = v * 100.
#   * Any other driver (custom property, quaternion, other ID, shape key value):
#       the constraint uses Evaluation Time (use_eval_time / eval_time). eval_time
#       is driven by an SDK f-curve with keys (v_min -> 0) and (v_max -> 1)
#       (linear, constant extrapolation), giving the same frame = v * 100.
# --------------------------------------------------------------------------

def action_fcurves(action):
    """F-Curve collection of an Action (legacy API, Blender 3.0 - 4.5)."""
    fcs = getattr(action, "fcurves", None)
    if fcs is None:
        raise RuntimeError("This Blender version no longer exposes Action.fcurves; Action mode is unavailable")
    return fcs


def action_transform_channel(s):
    """transform_channel enum for Location/Rotation/Scale drivers, or None when Evaluation Time is needed."""
    drv_id, _bone, rel_attr, idx, _path, _key = resolve_driver_target(s)
    if isinstance(drv_id, bpy.types.Object) and 0 <= idx <= 2:
        if rel_attr == "location":
            return ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")[idx]
        if rel_attr == "rotation_euler":
            return ("ROTATION_X", "ROTATION_Y", "ROTATION_Z")[idx]
        if rel_attr == "scale":
            return ("SCALE_X", "SCALE_Y", "SCALE_Z")[idx]
    return None


def action_constraint_name_for(scope_key):
    return "%s%s]" % (SDK_ACTION_CON_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_CON_PREFIX) + 1))


def action_name_for(scope_key):
    return "%s%s" % (SDK_ACTION_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_PREFIX)))


def get_action_for_driver(s, scope_key):
    """Return or create a dedicated Action for the current driver in Action Constraint mode."""
    act_name = action_name_for(scope_key)
    act = bpy.data.actions.get(act_name)
    if act is None:
        act = bpy.data.actions.new(act_name)
        if hasattr(act, "id_root"):
            try:
                act.id_root = "OBJECT"
            except (TypeError, AttributeError, ValueError):
                pass
        act.use_fake_user = True
    return act


def ensure_action_constraint(s, item, action, scope_key, drv_obj, drv_bone):
    """Ensure an Action constraint exists on the target bone or object for this driver axis."""
    owner = owner_of(item)
    if owner is None:
        return None, "Target owner not found"
    channel = action_transform_channel(s)
    con_name = action_constraint_name_for(scope_key)
    con = owner.constraints.get(con_name)
    created = con is None
    if created:
        con = owner.constraints.new("ACTION")
        con.name = con_name
        con.target_space = "LOCAL"
        if hasattr(con, "mix_mode"):
            try:
                con.mix_mode = "BEFORE_SPLIT"
            except (TypeError, ValueError, AttributeError):
                pass
        if hasattr(con, "show_expanded"):
            con.show_expanded = False

    if channel is None and not hasattr(con, "use_eval_time"):
        if created:
            owner.constraints.remove(con)
        return None, ("This Blender version has no Action Constraint evaluation time; "
                      "use a Location / Rotation / Scale driver channel")

    con.action = action
    if channel is not None:
        if hasattr(con, "use_eval_time"):
            con.use_eval_time = False
        con.target = drv_obj
        con.subtarget = drv_bone or ""
        con.transform_channel = channel
    else:
        # Evaluation Time: eval_time is driven by an SDK f-curve (see update_action_constraint_ranges)
        con.use_eval_time = True
        con.target = get_or_create_dummy_target()
        con.subtarget = ""
    con.mute = not s.live_preview
    return con, ""


def _eval_time_path(item, con_name):
    return full_path(item.bone, con_data_path(con_name) + ".eval_time")


def set_eval_time_curve(item, con_name, drv_obj, d_var_path, v_min, v_max, mute):
    """(Re)build the driver f-curve mapping driver value [v_min, v_max] -> eval_time [0, 1]."""
    fc, msg = ensure_driver(item.obj, _eval_time_path(item, con_name), -1, drv_obj, d_var_path, mute)
    if fc is None:
        _warn(msg)
        return
    kps = fc.keyframe_points
    while len(kps):
        kps.remove(kps[0], fast=True)
    key_curve(fc, v_min, 0.0, "LINEAR", "CONSTANT")
    key_curve(fc, v_max, 1.0, "LINEAR", "CONSTANT")


def remove_eval_time_driver(item, con_name):
    obj = item.obj
    if obj is None:
        return
    path = _eval_time_path(item, con_name)
    fc = find_driver_fcurve(obj, path, -1)
    if fc is not None and is_sdk_fcurve(fc):
        try:
            obj.driver_remove(path)
        except _RNA_ERRORS:
            pass


def update_action_constraint_ranges(action, s, scope_key, extra_items=()):
    """Update min/max, frame_start/end (and eval_time curves) of constraints using this action."""
    if not action:
        return
    try:
        fcs = action_fcurves(action)
    except RuntimeError:
        return
    points = [kp.co.x for fc in fcs for kp in fc.keyframe_points]
    if not points:
        return
    f_min = int(math.floor(min(points)))
    f_max = int(math.ceil(max(points)))
    if f_max - f_min < 1:
        f_max = f_min + int(ACTION_FRAMES_PER_UNIT)
    v_min = f_min / ACTION_FRAMES_PER_UNIT
    v_max = f_max / ACTION_FRAMES_PER_UNIT

    drv_id, _b, _a, _i, d_var_path, _k = resolve_driver_target(s)
    con_name = action_constraint_name_for(scope_key)
    seen = set()
    for item in list(s.driven_items) + list(extra_items):
        owner = owner_of(item)
        if owner is None or owner.as_pointer() in seen:
            continue
        seen.add(owner.as_pointer())
        con = owner.constraints.get(con_name)
        if con is None or con.action != action:
            continue
        con.frame_start = f_min
        con.frame_end = f_max
        if getattr(con, "use_eval_time", False):
            set_eval_time_curve(item, con_name, drv_id, d_var_path, v_min, v_max, not s.live_preview)
        else:
            con.min = v_min
            con.max = v_max


def key_action_mode(s, item, attr, idx, action, scope_key, drv_obj, drv_bone, x):
    """Key one channel into the Action for Action Constraint mode."""
    obj = item.obj
    if item.bone and obj == drv_obj and item.bone == drv_bone:
        return "Bone '%s' can't drive itself" % item.bone
    conflict = mode_conflict(item, "ACTION", attr, idx, scope_key)
    if conflict:
        return conflict
    path = full_path(item.bone, attr)
    try:
        y = get_value(obj, path, idx)
    except _RNA_ERRORS:
        return "Can't read '%s'" % var_path(path, idx)

    con, msg = ensure_action_constraint(s, item, action, scope_key, drv_obj, drv_bone)
    if con is None:
        return msg

    fcs = action_fcurves(action)
    fc = fcs.find(path, index=max(idx, 0))
    if fc is None:
        fc = fcs.new(path, index=max(idx, 0))

    add_rest_key(s, fc, x, rest_value_for(item, attr, idx), ACTION_FRAMES_PER_UNIT)
    key_curve(fc, x * ACTION_FRAMES_PER_UNIT, y, s.interpolation, s.extrapolation)
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
    """Return sorted unique list of keyed driver values for currently enabled driven channels."""
    drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
    if drv_id is None:
        return []
    values = set()
    if s.mode == "ACTION":
        act = bpy.data.actions.get(action_name_for(scope_key))
        if act:
            try:
                fcs = action_fcurves(act)
            except RuntimeError:
                return []
            for fc in fcs:
                for kp in fc.keyframe_points:
                    values.add(round(kp.co.x / ACTION_FRAMES_PER_UNIT, 4))
        return sorted(values)

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
    return sorted(values)


# Cache for the panel's "Keys:" line. Operators call invalidate_key_cache();
# undo / redo / file load do too (handlers below). Prev/Next Key always recompute.
_cache_gen = 0
_key_cache = {"token": None, "values": []}


def invalidate_key_cache(*_args):
    global _cache_gen
    _cache_gen += 1
    _key_cache["token"] = None


def cached_keyed_values(s, scope_key):
    token = (
        _cache_gen, s.as_pointer(), s.mode, scope_key,
        tuple(s.ch_loc), tuple(s.ch_rot), tuple(s.ch_quat), tuple(s.ch_scale), s.ch_custom,
        tuple((it.obj.as_pointer() if it.obj else 0, it.bone) for it in s.driven_items),
    )
    if _key_cache["token"] != token:
        try:
            values = get_all_keyed_values(s)
        except _READ_ERRORS:
            values = []
        _key_cache["token"] = token
        _key_cache["values"] = values
    return _key_cache["values"]


@persistent
def _on_history_change(*_args):
    invalidate_key_cache()


# --------------------------------------------------------------------------
# Mirror helpers
# --------------------------------------------------------------------------

_FLIP_WORDS = {"l": "r", "r": "l", "left": "right", "right": "left"}
_FLIP_SUFFIX_RE = re.compile(r"(?P<sep>[._-])(?P<word>L|R|left|right)(?P<tail>\.\d+)?$", re.IGNORECASE)
_FLIP_PREFIX_RE = re.compile(r"^(?P<word>L|R|left|right)(?P<sep>[._-])", re.IGNORECASE)


def _match_case(src, dst):
    if src.isupper():
        return dst.upper()
    if src.islower():
        return dst.lower()
    return dst.capitalize()


def flip_name(name):
    """.L<->.R, _L<->_R, .left<->.right (also -L, prefixes like L_, and .001 tails). '' if not sided."""
    m = _FLIP_SUFFIX_RE.search(name)
    if m:
        w = m.group("word")
        return (name[:m.start()] + m.group("sep") + _match_case(w, _FLIP_WORDS[w.lower()])
                + (m.group("tail") or ""))
    m = _FLIP_PREFIX_RE.match(name)
    if m:
        w = m.group("word")
        return _match_case(w, _FLIP_WORDS[w.lower()]) + m.group("sep") + name[m.end():]
    return ""


# Blender's own "Paste Pose Flipped" convention for X-mirrored bones.
_DEFAULT_MIRROR = {
    "location": (-1.0, 1.0, 1.0),
    "rotation_euler": (1.0, -1.0, -1.0),
    "rotation_quaternion": (1.0, 1.0, -1.0, -1.0),
    "scale": (1.0, 1.0, 1.0),
}


def mirror_signs(obj, src_bone, dst_bone):
    """Per-channel sign factors to mirror a pose from src_bone to dst_bone across armature X.

    Local channels map through A = R_dst^-1 * M * R_src (R = bone rest rotation,
    M = diag(-1, 1, 1)). For a signed-diagonal A the location factors are diag(A) and
    the Euler / quaternion-vector factors are -diag(A) (A is a reflection, det = -1).
    Returns (signs, exact); exact is False when the bones' rolls don't allow per-channel
    mirroring and Blender's standard X-mirror convention is used instead.
    """
    try:
        bones = obj.data.bones
        a = bones[src_bone].matrix_local.to_3x3()
        b = bones[dst_bone].matrix_local.to_3x3()
        lin = b.inverted() @ Matrix.Diagonal((-1.0, 1.0, 1.0)) @ a
    except _RNA_ERRORS:
        return _DEFAULT_MIRROR, False
    diag = [lin[i][i] for i in range(3)]
    off = max(abs(lin[i][j]) for i in range(3) for j in range(3) if i != j)
    if off > 0.1 or any(abs(abs(d) - 1.0) > 0.1 for d in diag):
        return _DEFAULT_MIRROR, False
    sx, sy, sz = (1.0 if d > 0 else -1.0 for d in diag)
    rot = (-sx, -sy, -sz)
    return {
        "location": (sx, sy, sz),
        "rotation_euler": rot,
        "rotation_quaternion": (1.0,) + rot,
        "scale": (1.0, 1.0, 1.0),
    }, True


def mirror_direct(s, item, dst, signs, drv_id, d_var_path, scope_key, problems):
    obj = item.obj
    mute = not s.live_preview
    curves = 0
    for attr, ax in sorted(enabled_channels(s)):
        src_fc = find_driver_fcurve(obj, full_path(item.bone, attr), ax)
        if src_fc is None or not is_sdk_fcurve(src_fc) or not driver_matches(src_fc, drv_id, d_var_path):
            continue
        conflict = mode_conflict(dst, "DIRECT", attr, ax, scope_key)
        if conflict:
            problems.append(conflict)
            continue
        dst_fc, msg = ensure_driver(obj, full_path(dst.bone, attr), ax, drv_id, d_var_path, mute)
        if dst_fc is None:
            problems.append(msg)
            continue
        copy_curve_flipped(src_fc, dst_fc, signs[attr][ax])
        curves += 1
    return curves, 0


def mirror_additive(s, item, dst, signs, drv_id, d_var_path, scope_key, problems):
    obj = item.obj
    mute = not s.live_preview
    curves = 0
    cons = set()
    for kind, ax in transform_specs(s):
        attr = DIRECT_ATTR[kind]
        src_targets = additive_targets(item, kind, ax, scope_key)
        src_fcs = [find_driver_fcurve(obj, p, i) for p, i in src_targets]
        if not any(f is not None and is_sdk_fcurve(f) and driver_matches(f, drv_id, d_var_path) for f in src_fcs):
            continue
        conflict = mode_conflict(dst, "ADDITIVE", attr, ax, scope_key)
        if conflict:
            problems.append(conflict)
            continue
        try:
            con = ensure_constraint(dst, kind, scope_key, mute)
        except _RNA_ERRORS as e:
            problems.append("Can't set up constraint on '%s': %s" % (dst.bone, e))
            continue
        sign = 1.0 if kind == "scale" else signs[attr][ax]
        for src_fc, (dp, di) in zip(src_fcs, additive_targets(dst, kind, ax, scope_key)):
            if src_fc is None or not is_sdk_fcurve(src_fc):
                continue
            dst_fc, msg = ensure_driver(obj, dp, di, drv_id, d_var_path, mute)
            if dst_fc is None:
                problems.append(msg)
                continue
            copy_curve_flipped(src_fc, dst_fc, sign)
            curves += 1
            cons.add(con.name)
    return curves, len(cons)


def mirror_action(s, item, dst, signs, drv_id, drv_bone, d_var_path, scope_key, action, problems):
    fcs = action_fcurves(action)
    enabled = enabled_channels(s)
    prefix = full_path(item.bone, "")
    curves = 0
    for fc in list(fcs):
        if not fc.data_path.startswith(prefix):
            continue
        attr = fc.data_path[len(prefix):]
        ax = fc.array_index
        if attr not in _CHANNEL_ATTRS or (attr, ax) not in enabled:
            continue
        conflict = mode_conflict(dst, "ACTION", attr, ax, scope_key)
        if conflict:
            problems.append(conflict)
            continue
        dst_path = full_path(dst.bone, attr)
        dst_fc = fcs.find(dst_path, index=ax)
        if dst_fc is None:
            dst_fc = fcs.new(dst_path, index=ax)
        copy_curve_flipped(fc, dst_fc, signs[attr][ax])
        curves += 1
    cons = 0
    if curves:
        con, msg = ensure_action_constraint(s, dst, action, scope_key, drv_id, drv_bone)
        if con is None:
            problems.append(msg)
        else:
            cons = 1
    return curves, cons


# --------------------------------------------------------------------------
# Configuration Data & Helpers
# --------------------------------------------------------------------------

def obj_ref(obj):
    """Name-based fallback description of an object: name, data name, library path."""
    if obj is None:
        return {}
    data = getattr(obj, "data", None)
    return {
        "name": obj.name,
        "data": data.name if data is not None else "",
        "lib": obj.library.filepath if obj.library else "",
    }


def resolve_config_object(pointer_obj, ref):
    """Pointer first, then name (+library), then unique data name. Returns (obj, how)."""
    if pointer_obj is not None:
        return pointer_obj, "pointer"
    name = ref.get("name", "")
    lib = ref.get("lib") or ""
    if name:
        if not lib:
            obj = bpy.data.objects.get(name)
        else:
            obj = next((o for o in bpy.data.objects
                        if o.name == name and o.library and o.library.filepath == lib), None)
        if obj is None:
            obj = bpy.data.objects.get(name)
        if obj is not None:
            return obj, "name"
    data_name = ref.get("data", "")
    if data_name:
        cands = [o for o in bpy.data.objects if o.data is not None and o.data.name == data_name]
        if len(cands) == 1:
            return cands[0], "data"
    return None, ""


def parse_driven_data(raw):
    """Normalise driven_data (new: list of dicts, legacy: list of [obj_name, bone])."""
    out = []
    for e in json.loads(raw or "[]"):
        if isinstance(e, dict):
            out.append({"obj": e.get("obj") or {}, "bone": e.get("bone", "")})
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            out.append({"obj": {"name": e[0]}, "bone": e[1]})
    return out


def _ref_label(ref, bone):
    return (ref.get("name") or "?") + ((":" + bone) if bone else "")


def _bools(vals, n):
    vals = [bool(v) for v in (vals or [])][:n]
    return tuple(vals + [False] * (n - len(vals)))


class SDK_ConfigObjRef(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)


class SDK_ConfigItem(PropertyGroup):
    name: StringProperty(name="Name", default="SDK_Config")
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_ref: StringProperty(default="{}")  # JSON {name, data, lib} fallback for driver_obj
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
    # Driven targets: JSON list of {"obj": {name, data, lib}, "bone": str} (legacy: [obj_name, bone])
    driven_data: StringProperty(default="[]")
    # Pointers parallel to driven_data entries (survive renames)
    driven_refs: CollectionProperty(type=SDK_ConfigObjRef)


def _preview_update(self, context):
    """Toggle SDK effect on/off so animators can inspect raw base values."""
    set_sdk_muted([item.obj for item in self.driven_items], not self.live_preview)


def _driver_changed(self, context):
    capture_driver_rest(self)


def _driver_current_get(self):
    try:
        return read_driver_value(self)
    except _READ_ERRORS:
        return 0.0


def _driver_current_set(self, value):
    try:
        write_driver_value(self, value)
        if bpy.context and bpy.context.view_layer:
            bpy.context.view_layer.update()
    except _READ_ERRORS:
        pass


class SDK_DrivenItem(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)
    bone: StringProperty()
    rest_loc: FloatVectorProperty(size=3)
    rest_euler: FloatVectorProperty(size=3)
    rest_quat: FloatVectorProperty(size=4, default=(1.0, 0.0, 0.0, 0.0))
    rest_scale: FloatVectorProperty(size=3, default=(1.0, 1.0, 1.0))
    # JSON {relative path: rest value} for custom driven channels (shape keys, ID props)
    rest_custom: StringProperty(default="{}")


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
        cfg.driver_ref = json.dumps(obj_ref(s.driver_obj))
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

        entries = []
        skipped = 0
        cfg.driven_refs.clear()
        for it in s.driven_items:
            if it.obj is None:
                skipped += 1
                continue
            entries.append({"obj": obj_ref(it.obj), "bone": it.bone})
            cfg.driven_refs.add().obj = it.obj
        cfg.driven_data = json.dumps(entries)

        s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Saved configuration '%s'" % cfg.name)
        if skipped:
            self.report({"WARNING"}, "%d driven item(s) had a missing object and were not saved" % skipped)
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
        missing = []
        notes = []

        try:
            drv_ref = json.loads(cfg.driver_ref or "{}")
        except ValueError:
            drv_ref = {}
        drv_obj, how = resolve_config_object(cfg.driver_obj, drv_ref)
        if drv_obj is None and (cfg.driver_obj is not None or drv_ref):
            missing.append("driver object '%s'" % (drv_ref.get("name") or "?"))
        elif how in ("name", "data"):
            notes.append("driver object found by %s" % how)

        # Order matters: channel / custom updates re-capture the rest, so rest is set last.
        s.driver_obj = drv_obj
        s.driver_bone = cfg.driver_bone
        if drv_obj is not None and cfg.driver_bone and (
                drv_obj.pose is None or cfg.driver_bone not in drv_obj.pose.bones):
            missing.append("driver bone '%s'" % cfg.driver_bone)
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

        # Restore driven items
        try:
            entries = parse_driven_data(cfg.driven_data)
        except (ValueError, TypeError) as e:
            entries = None
            self.report({"WARNING"}, "Could not read driven targets of '%s': %s" % (cfg.name, e))
        if entries is not None:
            s.driven_items.clear()
            for i, entry in enumerate(entries):
                ptr = cfg.driven_refs[i].obj if i < len(cfg.driven_refs) else None
                obj, how = resolve_config_object(ptr, entry["obj"])
                label = _ref_label(entry["obj"], entry["bone"])
                if obj is None:
                    missing.append(label)
                    continue
                bone = entry["bone"]
                if bone and (obj.pose is None or bone not in obj.pose.bones):
                    missing.append(label + " (bone not found)")
                    continue
                if how in ("name", "data"):
                    notes.append("'%s' found by %s" % (label, how))
                item = s.driven_items.add()
                item.obj = obj
                item.bone = bone
                capture(item, s)

        invalidate_key_cache()
        self.report({"INFO"}, "Loaded configuration '%s'" % cfg.name)
        if notes:
            self.report({"INFO"}, "; ".join(notes[:3]))
        if missing:
            self.report({"WARNING"}, "Missing in configuration '%s': %s" % (cfg.name, ", ".join(missing[:5])
                                                                           + (" ..." if len(missing) > 5 else "")))
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


def _config_to_dict(cfg):
    try:
        drv_ref = json.loads(cfg.driver_ref or "{}")
    except ValueError:
        drv_ref = {}
    if cfg.driver_obj is not None:
        drv_ref = obj_ref(cfg.driver_obj)
    driven = []
    try:
        entries = parse_driven_data(cfg.driven_data)
    except (ValueError, TypeError):
        entries = []
    for i, entry in enumerate(entries):
        ptr = cfg.driven_refs[i].obj if i < len(cfg.driven_refs) else None
        driven.append({"obj": obj_ref(ptr) if ptr is not None else entry["obj"], "bone": entry["bone"]})
    return {
        "name": cfg.name,
        "layer_tag": cfg.config_layer_tag,
        "mode": cfg.mode,
        "driver": {
            "obj": drv_ref, "bone": cfg.driver_bone, "channel": cfg.driver_channel,
            "custom": cfg.driver_custom, "rest": cfg.driver_rest,
        },
        "channels": {
            "loc": list(cfg.ch_loc), "rot": list(cfg.ch_rot), "quat": list(cfg.ch_quat),
            "scale": list(cfg.ch_scale), "custom": cfg.ch_custom,
        },
        "driven": driven,
    }


class SDK_OT_config_export(Operator, ExportHelper):
    bl_idname = "sdk.config_export"
    bl_label = "Export Configs"
    bl_description = "Save all configurations to a JSON file (objects are stored by name / data name / library)"
    bl_options = {"REGISTER"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        s = context.scene.sdk_settings
        payload = {
            "addon": "Set Driven Key",
            "version": list(bl_info["version"]),
            "configs": [_config_to_dict(c) for c in s.configs],
        }
        try:
            with open(self.filepath, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as e:
            self.report({"ERROR"}, "Could not write '%s': %s" % (self.filepath, e))
            return {"CANCELLED"}
        self.report({"INFO"}, "Exported %d configuration(s)" % len(s.configs))
        return {"FINISHED"}


class SDK_OT_config_import(Operator, ImportHelper):
    bl_idname = "sdk.config_import"
    bl_label = "Import Configs"
    bl_description = "Add configurations from a JSON file written by Export Configs"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        s = context.scene.sdk_settings
        try:
            with open(self.filepath, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            items = payload["configs"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.report({"ERROR"}, "Could not read configurations: %s" % e)
            return {"CANCELLED"}

        added = 0
        missing = []
        for c in items:
            try:
                drv = c.get("driver", {})
                ch = c.get("channels", {})
                cfg = s.configs.add()
                cfg.name = c.get("name", "Imported_SDK_Config")
                cfg.config_layer_tag = c.get("layer_tag", "")
                cfg.mode = c.get("mode", "ADDITIVE")
                drv_ref = drv.get("obj") or {}
                cfg.driver_ref = json.dumps(drv_ref)
                obj, _how = resolve_config_object(None, drv_ref)
                cfg.driver_obj = obj
                if obj is None and drv_ref:
                    missing.append("driver object '%s'" % (drv_ref.get("name") or "?"))
                cfg.driver_bone = drv.get("bone", "")
                cfg.driver_channel = drv.get("channel", "location:0")
                cfg.driver_custom = drv.get("custom", "")
                cfg.driver_rest = float(drv.get("rest", 0.0))
                cfg.ch_loc = _bools(ch.get("loc"), 3)
                cfg.ch_rot = _bools(ch.get("rot"), 3)
                cfg.ch_quat = _bools(ch.get("quat"), 4)
                cfg.ch_scale = _bools(ch.get("scale"), 3)
                cfg.ch_custom = ch.get("custom", "")
                entries = []
                for d in c.get("driven", []):
                    ref = d.get("obj") or {}
                    entries.append({"obj": ref, "bone": d.get("bone", "")})
                    d_obj, _how = resolve_config_object(None, ref)
                    cfg.driven_refs.add().obj = d_obj
                    if d_obj is None:
                        missing.append(_ref_label(ref, d.get("bone", "")))
                cfg.driven_data = json.dumps(entries)
                added += 1
            except (AttributeError, TypeError, ValueError, KeyError) as e:
                self.report({"WARNING"}, "Skipped a malformed configuration: %s" % e)
        if added:
            s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Imported %d configuration(s)" % added)
        if missing:
            self.report({"WARNING"}, "Not found in this file (kept by name): %s"
                        % (", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")))
        return {"FINISHED"} if added else {"CANCELLED"}


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
        invalidate_key_cache()
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
        if resolve_driver_target(s)[0] is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        try:
            write_driver_value(s, s.driver_rest)
        except _READ_ERRORS as e:
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
        if resolve_driver_target(s)[0] is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        try:
            cur = read_driver_value(s)
        except _READ_ERRORS:
            self.report({"ERROR"}, "Can't read driver value")
            return {"CANCELLED"}

        keys = get_all_keyed_values(s)  # always fresh, never the panel cache
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

        try:
            write_driver_value(s, target)
        except _READ_ERRORS as e:
            self.report({"ERROR"}, "Can't set driver value: %s" % e)
            return {"CANCELLED"}
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
            capture(item, s)
        invalidate_key_cache()
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

        # The mesh must be a driven item, otherwise keying never visits these paths.
        ptr = obj.as_pointer()
        item = next((i for i in s.driven_items if i.obj and i.obj.as_pointer() == ptr and not i.bone), None)
        added = item is None
        if added:
            item = s.driven_items.add()
            item.obj = obj
            item.bone = ""
            capture(item, s)  # base transform + current shape-key values
        else:
            capture_custom_rest(item, s, only_missing=True)
        invalidate_key_cache()
        self.report({"INFO"}, "Added %d shape key(s) to Custom driven channels%s"
                    % (len(keys), " and '%s' to the driven list" % obj.name if added else ""))
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
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_capture_base(Operator):
    bl_idname = "sdk.capture_base"
    bl_label = "Capture Base"
    bl_description = "Re-capture current channel values of driven items as the additive base"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        for item in s.driven_items:
            capture(item, s)
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
    bl_description = ("Copy and mirror SDK drivers and curves to counterpart bones (e.g. .L <-> .R) in the "
                      "current mode: Direct curves, Additive constraint layers or Action Constraint curves")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        del _WARNINGS[:]
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        action = None
        if s.mode == "ACTION":
            action = bpy.data.actions.get(action_name_for(scope_key))
            if action is None:
                self.report({"ERROR"}, "No Action exists for the current driver; key something first")
                return {"CANCELLED"}

        problems = []
        curves = cons = unpaired = 0
        done_pairs = set()
        dst_items = []
        for item in s.driven_items:
            obj = item.obj
            if not item.bone or obj is None or obj.pose is None:
                continue
            opp = flip_name(item.bone)
            if not opp or opp not in obj.pose.bones:
                unpaired += 1
                continue
            pair = (obj.as_pointer(), frozenset((item.bone, opp)))
            if pair in done_pairs:  # both sides loaded: the first one listed is the source
                continue
            done_pairs.add(pair)

            dst = SimpleNamespace(obj=obj, bone=opp)
            signs, exact = mirror_signs(obj, item.bone, opp)
            if not exact:
                problems.append("'%s' / '%s': rolls don't allow exact per-axis mirroring; "
                                "used the standard X-mirror signs" % (item.bone, opp))
            try:
                if s.mode == "DIRECT":
                    c, k = mirror_direct(s, item, dst, signs, drv_id, d_var_path, scope_key, problems)
                elif s.mode == "ADDITIVE":
                    c, k = mirror_additive(s, item, dst, signs, drv_id, d_var_path, scope_key, problems)
                else:
                    c, k = mirror_action(s, item, dst, signs, drv_id, drv_bone, d_var_path,
                                         scope_key, action, problems)
            except RuntimeError as e:
                problems.append(str(e))
                continue
            curves += c
            cons += k
            if c:
                dst_items.append(dst)

        if action is not None and dst_items:
            update_action_constraint_ranges(action, s, scope_key, dst_items)
        set_sdk_muted([i.obj for i in s.driven_items], not s.live_preview)
        invalidate_key_cache()
        context.view_layer.update()

        problems.extend(_WARNINGS)
        _report_problems(self, problems)
        if not curves:
            self.report({"WARNING"}, "Nothing mirrored: no keyed channels with a .L/.R (or _L/_R, .left/.right) "
                                     "counterpart bone for the enabled channels in %s mode" % s.mode.title())
            return {"CANCELLED"}
        msg = "Mirrored %d curve(s)" % curves
        if cons:
            msg += " on %d constraint(s)" % cons
        if unpaired:
            msg += " (%d bone(s) without counterpart)" % unpaired
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class SDK_OT_key(Operator):
    bl_idname = "sdk.key"
    bl_label = "Key"
    bl_description = "Store driven channels against current driver value (creates drivers/curves if needed)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        del _WARNINGS[:]
        drv_id, drv_bone, rel_attr, idx, d_var_path, scope_key = resolve_driver_target(s)
        if drv_id is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        try:
            x = read_driver_value(s)
        except _READ_ERRORS:
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
                if mode == "ADDITIVE":
                    msg = safe_call(key_additive, label, s, item, kind, axis,
                                    drv_id, d_var_path, idx, d_var_path, scope_key, x)
                elif mode == "DIRECT":
                    msg = safe_call(key_direct, label, s, item, DIRECT_ATTR[kind], axis,
                                    drv_id, d_var_path, idx, d_var_path, x)
                else:  # ACTION
                    msg = safe_call(key_action_mode, label, s, item, DIRECT_ATTR[kind], axis,
                                    action, scope_key, drv_id, drv_bone, x)
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
                                        action, scope_key, drv_id, drv_bone, x)
                    if msg:
                        problems.append(msg)
                    else:
                        keyed += 1

            for rel, c_idx in cspecs:
                label = "%s %s" % (item.bone or obj.name, rel)
                msg = safe_call(key_direct, label, s, item, rel, c_idx, drv_id, d_var_path, idx, d_var_path, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1

            if (mode == "ADDITIVE" or mode == "ACTION") and s.auto_reset and done:
                reset_base(item, done)

        if mode == "ACTION" and action:
            update_action_constraint_ranges(action, s, scope_key)

        set_sdk_muted([i.obj for i in s.driven_items], not s.live_preview)
        invalidate_key_cache()
        context.view_layer.update()
        problems.extend(_WARNINGS)
        _report_problems(self, problems)
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
            x = read_driver_value(s)
        except _READ_ERRORS:
            self.report({"ERROR"}, "Can't read driver property")
            return {"CANCELLED"}

        removed = 0
        if s.mode == "ACTION":
            act = bpy.data.actions.get(action_name_for(scope_key))
            if act:
                try:
                    fcs = action_fcurves(act)
                except RuntimeError as e:
                    self.report({"ERROR"}, str(e))
                    return {"CANCELLED"}
                frame = x * ACTION_FRAMES_PER_UNIT
                for fc in list(fcs):
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - frame) < KEY_EPS * ACTION_FRAMES_PER_UNIT:
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
        invalidate_key_cache()
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
            act = bpy.data.actions.get(action_name_for(scope_key))
            con_name = action_constraint_name_for(scope_key)
            for item in s.driven_items:
                owner = owner_of(item)
                if owner and con_name in owner.constraints:
                    remove_eval_time_driver(item, con_name)
                    owner.constraints.remove(owner.constraints[con_name])
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
                        target_id.driver_remove(rel_path, p_idx) if p_idx >= 0 else target_id.driver_remove(rel_path)
                    except _RNA_ERRORS:
                        continue
                    removed += 1
                    if ".constraints[" in path:
                        try:
                            set_by_path(obj, path, p_idx, identity_for(path))
                        except _RNA_ERRORS:
                            pass

                # Drop Transformation layers that no longer have any driver. Action constraints
                # (no drivers in transform mode) and user constraints are never touched here.
                owner = owner_of(item)
                if owner:
                    drivers = list(obj.animation_data.drivers) if obj.animation_data else []
                    for con in list(owner.constraints):
                        if is_sdk_transform_constraint(con):
                            prefix = full_path(item.bone, con_data_path(con.name))
                            if not any(fc.data_path.startswith(prefix) for fc in drivers):
                                owner.constraints.remove(con)

        cleanup_dummy_target()
        invalidate_key_cache()
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

        row = box.row(align=True)
        row.operator("sdk.config_export", text="Export Configs", icon="EXPORT")
        row.operator("sdk.config_import", text="Import Configs", icon="FILE_FOLDER")

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

            # Keyframe navigation helpers (list is cached, see cached_keyed_values)
            keys = cached_keyed_values(s, scope_key)
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
    SDK_ConfigObjRef,
    SDK_ConfigItem,
    SDK_DrivenItem,
    SDK_Settings,
    SDK_OT_config_add,
    SDK_OT_config_load,
    SDK_OT_config_remove,
    SDK_OT_config_export,
    SDK_OT_config_import,
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

_HANDLER_LISTS = (
    bpy.app.handlers.undo_post,
    bpy.app.handlers.redo_post,
    bpy.app.handlers.load_post,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)
    for handlers in _HANDLER_LISTS:
        if _on_history_change not in handlers:
            handlers.append(_on_history_change)


def unregister():
    for handlers in _HANDLER_LISTS:
        if _on_history_change in handlers:
            handlers.remove(_on_history_change)
    cleanup_dummy_target()  # only removed when no constraint still references it
    del bpy.types.Scene.sdk_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
