"""Set Driven Key — drivers."""

import math
import re

import bpy

from .const import DIRECT_ATTR, KEY_EPS, SDK_VAR, _READ_ERRORS, _RNA_ERRORS
from .util import _warn
from .paths import _is_array_property, _leaf_attr, _make_rna_error, full_path, identity_for, inspect_rna_target, resolve_id_and_path, set_by_path, var_path



def _driver_display_name(s, info):
    if s.driver_channel == "CUSTOM":
        if info.var_id is not None:
            base = info.var_id.name
            if info.bone:
                return "%s : %s" % (base, info.bone)
            if info.anim_id is not None and info.anim_id != info.var_id:
                return "%s (%s)" % (base, info.anim_id.bl_rna.name)
            return base
        return "Custom Path"
    if s.driver_obj:
        return s.driver_obj.name + (" : " + s.driver_bone if s.driver_bone else "")
    return "No driver loaded"


def resolve_driver_info(s):
    """Full resolution info for the current Driver settings."""
    layer = getattr(s, "config_layer_tag", "").strip()
    layer_tag = ("_" + re.sub(r"[^a-zA-Z0-9_]", "_", layer)) if layer else ""

    if s.driver_channel == "CUSTOM":
        raw = s.driver_custom.strip()
        info = inspect_rna_target(s.driver_obj, raw, bone=s.driver_bone)
        if info.var_id is not None:
            clean_sub = re.sub(r"[^a-zA-Z0-9_]", "_", info.var_data_path or raw).strip("_")
            head = info.bone or info.var_id.name
            scope_key = "%s_%s%s" % (head, clean_sub, layer_tag)
        else:
            clean_raw = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_") or "custom"
            scope_key = "%s%s" % (clean_raw, layer_tag)
        info.scope_key = scope_key
        info.label = _driver_display_name(s, info)
        return info

    attr, idx_str = s.driver_channel.split(":")
    idx = int(idx_str)
    if s.driver_obj is None:
        info = _make_rna_error("No driver loaded")
        info.scope_key = "obj_%s%s" % (s.driver_channel.replace(":", "_"), layer_tag)
        info.label = "No driver loaded"
        return info

    d_var_path = full_path(s.driver_bone, var_path(attr, idx))
    info = inspect_rna_target(s.driver_obj, d_var_path)
    b_name = s.driver_bone or s.driver_obj.name
    info.scope_key = "%s_%s%s" % (b_name, s.driver_channel.replace(":", "_"), layer_tag)
    info.label = _driver_display_name(s, info)
    return info


def resolve_driver_target(s):
    """Backwards-compatible 6-tuple wrapper around resolve_driver_info(s)."""
    info = resolve_driver_info(s)
    return info.var_id, info.bone, info.rel_attr, info.index, info.var_data_path, info.scope_key


def read_driver_value(s):
    """Current value of the driver property (raises on failure)."""
    info = resolve_driver_info(s)
    if not info.ok:
        raise LookupError(info.error or "No driver loaded")
    return info.value


def write_driver_value(s, value):
    """Write the driver property (raises on failure)."""
    info = resolve_driver_info(s)
    if not info.ok or info.var_id is None:
        raise LookupError(info.error or "No driver loaded")
    set_by_path(info.var_id, info.var_data_path, -1, value)
    try:
        info.var_id.update_tag()
        if info.anim_id and info.anim_id != info.var_id:
            info.anim_id.update_tag()
    except _RNA_ERRORS:
        pass


def owner_of(item):
    """The Object or PoseBone whose base channels this item represents."""
    if getattr(item, "target_kind", "OBJECT") != "OBJECT":
        return None
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
        'Any RNA path, e.g. ["myprop"], location[2], or bpy.data.materials["Mat"].node_tree.nodes["Value"].outputs[0].default_value',
    ))
    return items


DRIVER_ITEMS = _build_driver_items()


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
    return [t.strip() for t in s.ch_custom.split(";") if t.strip()]


def enabled_channels(s):
    """{(attr, axis)} for the enabled Location / Rotation / Scale / Quaternion toggles."""
    out = {(DIRECT_ATTR[k], a) for k, a in transform_specs(s)}
    out.update(("rotation_quaternion", i) for i in quat_specs(s))
    return out


def has_object_targets(s):
    return any(it.target_kind == "OBJECT" for it in s.driven_items)


def has_bone_targets(s):
    return any(it.target_kind == "OBJECT" and it.bone for it in s.driven_items)


def capture_driver_rest(s):
    """Remember the driver's current value as its rest value."""
    try:
        s.driver_rest = read_driver_value(s)
    except _READ_ERRORS:
        pass


def auto_scrub_range(s):
    """Pick a usable Min/Max for the scrub slider from the driver channel and its current value."""
    info = resolve_driver_info(s)
    attr = _leaf_attr(info.anim_path or info.rel_attr)
    lo, hi = -1.0, 1.0
    if attr == "rotation_euler":
        lo, hi = -math.pi, math.pi
    elif attr == "scale":
        lo, hi = 0.0, 2.0
    elif attr == "location":
        lo, hi = -2.0, 2.0
    elif info.ok and info.value is not None:
        v = info.value
        if 0.0 <= v <= 1.0:
            lo, hi = 0.0, 1.0
        elif v > 1.0:
            lo, hi = 0.0, max(2.0, v * 2.0)
        elif v < -1.0:
            lo, hi = min(-2.0, v * 2.0), 0.0
    if info.ok and info.value is not None:
        lo, hi = min(lo, info.value), max(hi, info.value)
    s.scrub_min = lo
    s.scrub_max = hi


def find_driver_fcurve(target_id, path, index):
    """Fast driver lookup using drivers.find(path, index)."""
    if target_id is None:
        return None
    ad = getattr(target_id, "animation_data", None)
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


def driver_matches(fc, drv_id, d_var_path):
    """True if the SDK f-curve is driven by exactly this driver property."""
    var = next((v for v in fc.driver.variables if v.name == SDK_VAR), None)
    if var is None:
        return False
    tgt = var.targets[0]
    return tgt.id == drv_id and tgt.data_path == d_var_path


def _safe_id_type(driver_target, id_block):
    valid = set(driver_target.bl_rna.properties["id_type"].enum_items.keys())
    t = getattr(id_block, "id_type", None)
    if t in valid:
        return t
    aliases = {"LIGHT": "LAMP", "COLLECTION": "GROUP"}
    if t in aliases and aliases[t] in valid:
        return aliases[t]
    rna_name = getattr(getattr(id_block, "rna_type", None), "name", "").upper().replace(" ", "")
    if rna_name in valid:
        return rna_name
    return "OBJECT"


def ensure_driver_on_id(target_id, rel_path, index, drv_id, drv_data_path, mute):
    """Return (fcurve, message) on an arbitrary ID datablock. fcurve is None if channel can't be driven."""
    if target_id is None:
        return None, "Target datablock not found"
    fc = find_driver_fcurve(target_id, rel_path, index)
    if fc is None:
        if index < 0 and _is_array_property(target_id, rel_path):
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        ad = getattr(target_id, "animation_data", None)
        before = {f.array_index for f in ad.drivers if f.data_path == rel_path} if ad else set()
        try:
            fc = target_id.driver_add(rel_path, index) if index >= 0 else target_id.driver_add(rel_path)
        except _RNA_ERRORS as e:
            return None, "Cannot drive '%s': %s" % (rel_path, e)
        if isinstance(fc, (list, tuple)):
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
        and var.targets[0].id == drv_id
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
        t.id_type = _safe_id_type(t, drv_id)
        t.id = drv_id
        t.data_path = drv_data_path
        drv.type = "SUM"

    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    return fc, ""


def ensure_driver(obj, path, index, drv_obj, drv_data_path, mute):
    """Wrapper resolving (obj, path) across ID boundaries before calling ensure_driver_on_id."""
    target_id, rel_path = resolve_id_and_path(obj, path)
    return ensure_driver_on_id(target_id, rel_path, index, drv_obj, drv_data_path, mute)


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
    """Key the rest state (driver at its rest value) before / together with the posed key."""
    if rest_y is None or not s.auto_rest_key:
        return False
    if abs(x - s.driver_rest) < KEY_EPS:
        return False
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


def _remove_driver_target(target_id, rel_path, p_idx, is_con):
    fc = find_driver_fcurve(target_id, rel_path, p_idx)
    if fc is None or not is_sdk_fcurve(fc):
        return False
    try:
        target_id.driver_remove(rel_path, p_idx) if p_idx >= 0 else target_id.driver_remove(rel_path)
    except _RNA_ERRORS:
        return False
    if is_con and ".constraints[" in rel_path:
        try:
            set_by_path(target_id, rel_path, p_idx, identity_for(rel_path))
        except _RNA_ERRORS:
            pass
    return True
