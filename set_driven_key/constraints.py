"""Set Driven Key — constraints."""

import json
import math

from mathutils import Euler, Quaternion

from .const import CON_SUFFIX, DIRECT_ATTR, SDK_ACTION_CON_PREFIX, SDK_CON_BASE, _ATTR_KIND, _BONE_SUBPATH_RE, _CHANNEL_ATTRS, _RNA_ERRORS, _SDK_NAME_TAIL_RE, _SDK_TRANSFORM_PREFIXES
from .util import _fit_scope, _warn
from .paths import con_data_path, full_path, get_value, inspect_rna_target, var_path
from .drivers import add_rest_key, custom_specs, ensure_driver_on_id, find_driver_fcurve, is_sdk_fcurve, key_curve, owner_of, quat_specs, transform_specs
from .dummy import _constraint_owners, get_or_create_dummy_target



def is_sdk_transform_constraint(con):
    return (con.type == "TRANSFORM" and con.name.startswith(_SDK_TRANSFORM_PREFIXES)
            and _SDK_NAME_TAIL_RE.search(con.name) is not None)


def is_sdk_action_constraint(con):
    return (con.type == "ACTION" and con.name.startswith(SDK_ACTION_CON_PREFIX)
            and _SDK_NAME_TAIL_RE.search(con.name) is not None)


def find_sdk_constraint(owner, name, type_):
    """The constraint called `name`, or Blender's auto-renamed twin (`name.001`, `name.002`...)."""
    con = owner.constraints.get(name)
    if con is not None:
        return con
    twins = [c for c in owner.constraints
             if c.type == type_ and c.name.startswith(name + ".") and c.name[len(name) + 1:].isdigit()]
    return min(twins, key=lambda c: c.name) if twins else None


def is_sdk_constraint(con):
    return is_sdk_transform_constraint(con) or is_sdk_action_constraint(con)


def _sdk_id_blocks(obj):
    blocks = [obj]
    keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if keys is not None:
        blocks.append(keys)
    return blocks


def _mute_id_sdk_drivers(id_block, muted, seen_ids):
    if id_block is None:
        return
    ptr = id_block.as_pointer()
    if ptr in seen_ids:
        return
    seen_ids.add(ptr)
    ad = getattr(id_block, "animation_data", None)
    if ad:
        for fc in ad.drivers:
            if is_sdk_fcurve(fc):
                fc.mute = muted


def set_sdk_muted_for_settings(s, muted):
    """Mute / unmute every SDK driver and SDK constraint referenced by `s.driven_items`."""
    seen_ids = set()
    seen_objs = set()
    cspecs = custom_specs(s)
    for item in s.driven_items:
        if item.target_kind == "RNA_PATH":
            info = inspect_rna_target(None, item.rna_path)
            if info.anim_id is not None:
                _mute_id_sdk_drivers(info.anim_id, muted, seen_ids)
            continue

        obj = item.obj
        if obj is None:
            continue
        for block in _sdk_id_blocks(obj):
            _mute_id_sdk_drivers(block, muted, seen_ids)
        for rel in cspecs:
            info = inspect_rna_target(obj, rel, bone=item.bone)
            if info.anim_id is not None:
                _mute_id_sdk_drivers(info.anim_id, muted, seen_ids)
        if obj.as_pointer() not in seen_objs:
            seen_objs.add(obj.as_pointer())
            for owner in _constraint_owners(obj):
                for con in owner.constraints:
                    if is_sdk_constraint(con):
                        con.mute = muted


# --------------------------------------------------------------------------
# Rest values
# --------------------------------------------------------------------------

def capture(item, s=None):
    """Snapshot base channels / RNA values for an item."""
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.ok and info.value is not None:
            item.rest_rna = info.value
            item.has_rest_rna = True
        return

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
    """Remember the current value of `ch_custom` driven channels for an OBJECT item."""
    if item.target_kind != "OBJECT":
        return
    obj = item.obj
    if obj is None:
        return
    try:
        store = json.loads(item.rest_custom or "{}")
    except ValueError:
        store = {}
    for rel in custom_specs(s):
        if only_missing and rel in store:
            continue
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.ok and info.value is not None:
            store[rel] = info.value
    item.rest_custom = json.dumps(store)


def rest_value_for(item, attr, idx, raw_key=None):
    """Rest value of a driven channel, or None if unknown."""
    if item.target_kind == "RNA_PATH":
        if item.has_rest_rna:
            return item.rest_rna
        if ".value" in item.rna_path and "key_blocks[" in item.rna_path:
            return 0.0
        return None

    if raw_key is None and idx >= 0:
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
    for k in (raw_key, var_path(attr, idx)):
        if k and k in store:
            return float(store[k])
    if attr.endswith(".value") and "key_blocks[" in attr:
        return 0.0
    return None


# --------------------------------------------------------------------------
# Additive mode helpers (Multi-axis scoped constraints)
# --------------------------------------------------------------------------

def constraint_name_for(kind, scope_key):
    base = SDK_CON_BASE[kind]
    return "%s [%s]" % (base, _fit_scope(scope_key, len(base) + 3))


def actual_constraint_name(item, kind, scope_key):
    name = constraint_name_for(kind, scope_key)
    owner = owner_of(item)
    if owner is not None:
        con = find_sdk_constraint(owner, name, "TRANSFORM")
        if con is not None:
            return con.name
    return name


def additive_targets(item, kind, axis, scope_key):
    con_name = actual_constraint_name(item, kind, scope_key)
    a = "xyz"[axis]
    rel = con_data_path(con_name) + "."
    return [
        (full_path(item.bone, rel + "to_%s_%s%s" % (m, a, CON_SUFFIX[kind])), -1)
        for m in ("min", "max")
    ]


def ensure_constraint(item, kind, scope_key, mute):
    owner = owner_of(item)
    con_name = constraint_name_for(kind, scope_key)
    con = find_sdk_constraint(owner, con_name, "TRANSFORM")
    if con is None:
        con = owner.constraints.new("TRANSFORM")
        con.name = con_name
        if con.name != con_name:
            _warn("Blender renamed constraint '%s' to '%s' (name already used in the pose); using that name"
                  % (con_name, con.name))
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
# Mode conflict detection & Keying
# --------------------------------------------------------------------------

def mode_conflict(item, mode, attr, axis, scope_key=""):
    from .action import action_find_fcurve
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

    if mode == "DIRECT" and attr in _ATTR_KIND:
        kind = _ATTR_KIND[attr]
        prefix = SDK_CON_BASE[kind] + " ["
        for con in owner.constraints:
            if not is_sdk_transform_constraint(con) or not con.name.startswith(prefix):
                continue
            for p, i in additive_targets(item, kind, axis, con.name[len(prefix):con.name.rfind("]")]):
                fc = find_driver_fcurve(obj, p, i)
                if fc is not None and is_sdk_fcurve(fc) and len(fc.keyframe_points):
                    return "%s already has the Additive SDK layer '%s' (use Delete Curves first)" % (label, con.name)

    if mode == "DIRECT":
        for con in owner.constraints:
            if not is_sdk_action_constraint(con) or con.action is None:
                continue
            try:
                found = action_find_fcurve(con.action, path, axis)
            except RuntimeError:
                break
            if found is not None:
                return "%s already has the Action SDK constraint '%s' (use Delete Curves first)" % (label, con.name)
    return ""


def key_additive(s, item, kind, axis, drv_info, scope_key, x):
    """Key one transform channel as an additive offset."""
    obj = item.obj
    owner = owner_of(item)
    if item.bone and obj == drv_info.var_id and item.bone == drv_info.bone:
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
        if obj == drv_info.anim_id and path == drv_info.anim_path and p_idx == drv_info.index:
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

    targets = additive_targets(item, kind, axis, scope_key)
    for path, p_idx in targets:
        fc, msg = ensure_driver_on_id(obj, path, p_idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
        if fc is None:
            return msg
        add_rest_key(s, fc, x, ident)
        key_curve(fc, x, value, s.interpolation, s.extrapolation)
    return ""


def key_direct(s, item, attr, idx, drv_info, x):
    """Maya-style direct channel drive for standard Object/Bone channels."""
    obj = item.obj
    path = full_path(item.bone, attr)
    if obj == drv_info.anim_id and path == drv_info.anim_path and idx == drv_info.index:
        return "'%s' can't drive itself" % var_path(path, idx)
    conflict = mode_conflict(item, "DIRECT", attr, idx)
    if conflict:
        return conflict
    try:
        y = get_value(obj, path, idx)
    except _RNA_ERRORS:
        return "Can't read '%s'" % var_path(path, idx)
    fc, msg = ensure_driver_on_id(obj, path, idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
    if fc is None:
        return msg

    add_rest_key(s, fc, x, rest_value_for(item, attr, idx))
    key_curve(fc, x, y, s.interpolation, s.extrapolation)
    return ""


def key_direct_rna(s, item, target_info, drv_info, x, raw_key=None):
    """Direct drive for any resolved RNA target (any ID block, custom prop, node, shape key, etc.)."""
    if not target_info.ok:
        return target_info.error or "Invalid RNA path"

    anim_id = target_info.anim_id
    anim_path = target_info.anim_path
    idx = target_info.index

    if anim_id == drv_info.anim_id and anim_path == drv_info.anim_path and idx == drv_info.index:
        return "'%s' can't drive itself" % target_info.var_data_path

    if item.target_kind == "OBJECT" and anim_id == item.obj:
        # Check conflict if user manually entered a transform channel via Custom
        raw_attr = anim_path
        if item.bone and anim_path.startswith('pose.bones['):
            m = _BONE_SUBPATH_RE.match(anim_path)
            if m and m.group("dot"):
                raw_attr = m.group("dot")
        conflict = mode_conflict(item, "DIRECT", raw_attr, idx)
        if conflict:
            return conflict

    fc, msg = ensure_driver_on_id(anim_id, anim_path, idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
    if fc is None:
        return msg

    rest_y = rest_value_for(item, anim_path, idx, raw_key=raw_key)
    add_rest_key(s, fc, x, rest_y)
    key_curve(fc, x, target_info.value, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------

def direct_resolved_targets(s, item, scope_key=""):
    """Yield (target_id, rel_path, index, is_constraint_prop) for every enabled target on `item`."""
    out = []
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, False))
        return out

    obj = item.obj
    if obj is None:
        return out

    for kind, axis in transform_specs(s):
        out.append((obj, full_path(item.bone, DIRECT_ATTR[kind]), axis, False))
        if scope_key:
            for p, p_idx in additive_targets(item, kind, axis, scope_key):
                out.append((obj, p, p_idx, True))
    for i in quat_specs(s):
        out.append((obj, full_path(item.bone, "rotation_quaternion"), i, False))
    for rel in custom_specs(s):
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, ".constraints[" in info.anim_path))
    return out


def custom_direct_resolved_targets(s, item):
    """Yield (target_id, rel_path, index, is_constraint_prop) for Direct-only custom/RNA targets."""
    out = []
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, False))
        return out

    obj = item.obj
    if obj is None:
        return out
    for rel in custom_specs(s):
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, ".constraints[" in info.anim_path))
    return out
