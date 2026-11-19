"""Set Driven Key — action."""

import math
from types import SimpleNamespace

import bpy
from mathutils import Quaternion

from .const import ACTION_FRAMES_PER_UNIT, SDK_ACTION_CON_PREFIX, SDK_ACTION_PREFIX, _BONE_SUBPATH_RE, _QUAT_IDENT, _RNA_ERRORS
from .util import _fit_scope, _warn
from .paths import con_data_path, full_path, var_path
from .drivers import add_rest_key, ensure_driver_on_id, find_driver_fcurve, is_sdk_fcurve, key_curve, owner_of
from .dummy import get_or_create_dummy_target
from .constraints import current_euler, find_sdk_constraint, mode_conflict, rest_euler



# --------------------------------------------------------------------------
# Action Constraint Mode Helpers
# --------------------------------------------------------------------------

def _is_layered_only(action):
    return getattr(action, "fcurves", None) is None and hasattr(action, "slots")


def _action_slot(action, create=False):
    slots = getattr(action, "slots", None)
    if slots is None:
        return None
    if len(slots):
        return slots[0]
    if create:
        return slots.new(id_type="OBJECT", name="SDK")
    return None


def _action_channelbag(action, create=False):
    slot = _action_slot(action, create)
    if slot is None:
        return None
    if not len(action.layers):
        if not create:
            return None
        action.layers.new(name="SDK")
    layer = action.layers[0]
    if not len(layer.strips):
        if not create:
            return None
        layer.strips.new(type="KEYFRAME")
    return layer.strips[0].channelbag(slot, ensure=create)


def action_fcurves(action, create=False):
    fcs = getattr(action, "fcurves", None)
    if fcs is not None:
        return fcs
    if not hasattr(action, "slots"):
        raise RuntimeError("Unsupported Blender version: Action exposes neither fcurves nor slots")
    bag = _action_channelbag(action, create)
    return bag.fcurves if bag is not None else ()


def action_find_fcurve(action, path, index):
    fcs = action_fcurves(action)
    return fcs.find(path, index=index) if hasattr(fcs, "find") else None


def action_transform_channel(drv_info):
    if isinstance(drv_info.var_id, bpy.types.Object) and 0 <= drv_info.index <= 2:
        # Check that attribute on the Object / PoseBone is directly location/rotation_euler/scale
        tail = drv_info.anim_path
        if drv_info.bone:
            m = _BONE_SUBPATH_RE.match(drv_info.anim_path)
            tail = (m.group("dot") or "") if m else ""
        if tail == "location":
            return ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")[drv_info.index]
        if tail == "rotation_euler":
            return ("ROTATION_X", "ROTATION_Y", "ROTATION_Z")[drv_info.index]
        if tail == "scale":
            return ("SCALE_X", "SCALE_Y", "SCALE_Z")[drv_info.index]
    return None


def action_constraint_name_for(scope_key):
    return "%s%s]" % (SDK_ACTION_CON_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_CON_PREFIX) + 1))


def action_name_for(scope_key, id_name=""):
    """Action name for a driver scope. With `id_name` (the driver's ID name) the name is
    unique per driver object, so duplicated rigs don't share one Action."""
    if id_name:
        scope_key = "%s__%s" % (id_name, scope_key)
    return "%s%s" % (SDK_ACTION_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_PREFIX)))


def find_action_for_driver(drv_info):
    """Existing SDK Action for this driver: the per-object name first, then the pre-1.7.2 name."""
    var_id = getattr(drv_info, "var_id", None)
    if var_id is not None:
        act = bpy.data.actions.get(action_name_for(drv_info.scope_key, var_id.name))
        if act is not None:
            return act
    return bpy.data.actions.get(action_name_for(drv_info.scope_key))


def get_action_for_driver(scope_key, id_name="", drv_info=None):
    act = find_action_for_driver(drv_info) if drv_info is not None else None
    if act is None and not id_name:
        act = bpy.data.actions.get(action_name_for(scope_key))
    act_name = action_name_for(scope_key, id_name)
    if act is None:
        act = bpy.data.actions.new(act_name)
        if hasattr(act, "id_root"):
            try:
                act.id_root = "OBJECT"
            except (TypeError, AttributeError, ValueError):
                pass
        act.use_fake_user = True
    if _is_layered_only(act):
        action_fcurves(act, create=True)
    return act


def _sync_action_slot(con, action):
    """Point the constraint at the Action's slot (Blender 4.4+). The slot may only exist
    after the first curve has been created, so this is re-run after keying."""
    if not hasattr(con, "action_slot"):
        return
    slot = _action_slot(action, create=_is_layered_only(action))
    if slot is not None and con.action_slot != slot:
        try:
            con.action_slot = slot
        except _RNA_ERRORS:
            pass


def ensure_action_constraint(s, item, action, scope_key, drv_info):
    owner = owner_of(item)
    if owner is None:
        return None, "Target owner not found"
    channel = action_transform_channel(drv_info)
    con_name = action_constraint_name_for(scope_key)
    con = find_sdk_constraint(owner, con_name, "ACTION")
    created = con is None
    if created:
        con = owner.constraints.new("ACTION")
        con.name = con_name
        if con.name != con_name:
            _warn("Blender renamed constraint '%s' to '%s' (name already used in the pose); using that name"
                  % (con_name, con.name))
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
    _sync_action_slot(con, action)
    if channel is not None:
        if hasattr(con, "use_eval_time"):
            con.use_eval_time = False
        con.target = drv_info.var_id
        con.subtarget = drv_info.bone or ""
        con.transform_channel = channel
    else:
        con.use_eval_time = True
        con.target = get_or_create_dummy_target()
        con.subtarget = ""
    con.mute = not s.live_preview
    return con, ""


def _eval_time_path(item, con_name):
    return full_path(item.bone, con_data_path(con_name) + ".eval_time")


def set_eval_time_curve(item, con_name, drv_id, d_var_path, v_min, v_max, mute):
    fc, msg = ensure_driver_on_id(item.obj, _eval_time_path(item, con_name), -1, drv_id, d_var_path, mute)
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


def update_action_constraint_ranges(action, s, drv_info, extra_items=()):
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

    con_name = action_constraint_name_for(drv_info.scope_key)
    seen = set()
    for src in list(s.driven_items) + list(extra_items):
        if getattr(src, "target_kind", "OBJECT") != "OBJECT":
            continue
        obj = src.obj
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        owners = [("", obj)] + [(pb.name, pb) for pb in (obj.pose.bones if obj.pose else ())]
        for bone, owner in owners:
            con = find_sdk_constraint(owner, con_name, "ACTION")
            if con is None or con.action != action:
                continue
            _sync_action_slot(con, action)
            con.frame_start = f_min
            con.frame_end = f_max
            if getattr(con, "use_eval_time", False):
                set_eval_time_curve(SimpleNamespace(obj=obj, bone=bone), con.name,
                                    drv_info.var_id, drv_info.var_data_path,
                                    v_min, v_max, not s.live_preview)
            else:
                k = math.degrees(1.0) if str(con.transform_channel).startswith("ROTATION") else 1.0
                con.min = v_min * k
                con.max = v_max * k


def action_channel_value(item, owner, attr, idx):
    if attr == "location":
        return owner.location[idx] - item.rest_loc[idx], 0.0
    if attr == "rotation_euler":
        if owner.rotation_mode == "AXIS_ANGLE":
            raise ValueError("Axis-angle rotation isn't supported")
        off = current_euler(owner)[idx] - rest_euler(item, owner)[idx]
        if owner.rotation_mode == "QUATERNION":
            off = (off + math.pi) % (2.0 * math.pi) - math.pi
        return off, 0.0
    if attr == "scale":
        rest = item.rest_scale[idx]
        if abs(rest) < 1e-8:
            raise ValueError("Base scale is zero on %s, can't compute a ratio" % (item.bone or item.obj.name))
        return owner.scale[idx] / rest, 1.0
    if attr == "rotation_quaternion":
        delta = owner.rotation_quaternion @ Quaternion(item.rest_quat).inverted()
        return delta[idx], _QUAT_IDENT[idx]
    raise ValueError("Unsupported channel '%s'" % attr)


def action_rotation_skip(item, wants_euler, other_enabled):
    owner = owner_of(item)
    quat_bone = owner.rotation_mode == "QUATERNION"
    if wants_euler == (not quat_bone):
        return None
    if other_enabled:
        return ""
    name = item.bone or item.obj.name
    if quat_bone:
        return "'%s' uses quaternion rotation: enable the Quat channels (Euler curves don't affect it)" % name
    return "'%s' uses Euler rotation: enable the Rotation channels (quaternion curves don't affect it)" % name


def new_action_fcurve(action, fcs, path, index, group=""):
    key = "group_name" if _is_layered_only(action) else "action_group"
    if group:
        try:
            return fcs.new(path, index=index, **{key: group})
        except TypeError:
            pass
    return fcs.new(path, index=index)


def key_action_mode(s, item, attr, idx, action, scope_key, drv_info, x):
    obj = item.obj
    if item.bone and obj == drv_info.var_id and item.bone == drv_info.bone:
        return "Bone '%s' can't drive itself" % item.bone
    conflict = mode_conflict(item, "ACTION", attr, idx, scope_key)
    if conflict:
        return conflict
    path = full_path(item.bone, attr)
    try:
        y, ident = action_channel_value(item, owner_of(item), attr, idx)
    except _RNA_ERRORS as e:
        return "Can't read '%s': %s" % (var_path(path, idx), e)

    con, msg = ensure_action_constraint(s, item, action, scope_key, drv_info)
    if con is None:
        return msg

    fcs = action_fcurves(action, create=True)
    fc = fcs.find(path, index=max(idx, 0))
    # With Live Preview on, the viewport pose already includes this curve. The RNA
    # channel does not, so add the current curve value back or the key drifts.
    if s.live_preview and fc is not None and len(fc.keyframe_points) and attr != "rotation_quaternion":
        try:
            existing = float(fc.evaluate(x * ACTION_FRAMES_PER_UNIT))
        except _RNA_ERRORS:
            existing = ident
        if attr == "scale":
            y *= existing
        else:
            y += existing
    if fc is None:
        fc = new_action_fcurve(action, fcs, path, max(idx, 0), item.bone or "Object Transforms")

    add_rest_key(s, fc, x, ident, ACTION_FRAMES_PER_UNIT)
    key_curve(fc, x * ACTION_FRAMES_PER_UNIT, y, s.interpolation, s.extrapolation)
    return ""
