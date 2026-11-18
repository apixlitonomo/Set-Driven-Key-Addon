"""Set Driven Key — mirror."""

from mathutils import Matrix

from .const import DIRECT_ATTR, _CHANNEL_ATTRS, _DEFAULT_MIRROR, _FLIP_PREFIX_RE, _FLIP_SUFFIX_RE, _FLIP_WORDS, _RNA_ERRORS
from .paths import full_path
from .drivers import copy_curve_flipped, driver_matches, enabled_channels, ensure_driver_on_id, find_driver_fcurve, is_sdk_fcurve, transform_specs
from .constraints import additive_targets, ensure_constraint, mode_conflict
from .action import action_fcurves, ensure_action_constraint, new_action_fcurve



def _match_case(src, dst):
    if src.isupper():
        return dst.upper()
    if src.islower():
        return dst.lower()
    return dst.capitalize()


def flip_name(name):
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


def mirror_signs(obj, src_bone, dst_bone):
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


def mirror_direct(s, item, dst, signs, drv_info, problems):
    obj = item.obj
    mute = not s.live_preview
    curves = 0
    for attr, ax in sorted(enabled_channels(s)):
        src_fc = find_driver_fcurve(obj, full_path(item.bone, attr), ax)
        if src_fc is None or not is_sdk_fcurve(src_fc) or not driver_matches(src_fc, drv_info.var_id, drv_info.var_data_path):
            continue
        conflict = mode_conflict(dst, "DIRECT", attr, ax, drv_info.scope_key)
        if conflict:
            problems.append(conflict)
            continue
        dst_fc, msg = ensure_driver_on_id(obj, full_path(dst.bone, attr), ax, drv_info.var_id, drv_info.var_data_path, mute)
        if dst_fc is None:
            problems.append(msg)
            continue
        copy_curve_flipped(src_fc, dst_fc, signs[attr][ax])
        curves += 1
    return curves, 0


def mirror_additive(s, item, dst, signs, drv_info, problems):
    obj = item.obj
    mute = not s.live_preview
    scope_key = drv_info.scope_key
    curves = 0
    cons = set()
    for kind, ax in transform_specs(s):
        attr = DIRECT_ATTR[kind]
        src_targets = additive_targets(item, kind, ax, scope_key)
        src_fcs = [find_driver_fcurve(obj, p, i) for p, i in src_targets]
        if not any(f is not None and is_sdk_fcurve(f) and driver_matches(f, drv_info.var_id, drv_info.var_data_path) for f in src_fcs):
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
            dst_fc, msg = ensure_driver_on_id(obj, dp, di, drv_info.var_id, drv_info.var_data_path, mute)
            if dst_fc is None:
                problems.append(msg)
                continue
            copy_curve_flipped(src_fc, dst_fc, sign)
            curves += 1
            cons.add(con.name)
    return curves, len(cons)


def mirror_action(s, item, dst, signs, drv_info, action, problems):
    fcs = action_fcurves(action, create=True)
    enabled = enabled_channels(s)
    prefix = full_path(item.bone, "")
    scope_key = drv_info.scope_key
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
            dst_fc = new_action_fcurve(action, fcs, dst_path, ax, dst.bone)
        copy_curve_flipped(fc, dst_fc, signs[attr][ax])
        curves += 1
    cons = 0
    if curves:
        con, msg = ensure_action_constraint(s, dst, action, scope_key, drv_info)
        if con is None:
            problems.append(msg)
        else:
            cons = 1
    return curves, cons
