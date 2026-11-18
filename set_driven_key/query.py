"""Set Driven Key — query."""

import bpy
from bpy.app.handlers import persistent

from .const import ACTION_FRAMES_PER_UNIT, _READ_ERRORS
from .drivers import find_driver_fcurve, is_sdk_fcurve, resolve_driver_info
from .constraints import custom_direct_resolved_targets, direct_resolved_targets
from .action import action_fcurves, action_name_for



def get_all_keyed_values(s):
    """Return sorted unique list of keyed driver values for currently enabled driven channels."""
    drv_info = resolve_driver_info(s)
    if drv_info.var_id is None:
        return []
    scope_key = drv_info.scope_key
    values = set()
    add = values.add
    find = find_driver_fcurve
    is_sdk = is_sdk_fcurve

    if s.mode == "ACTION":
        scale = ACTION_FRAMES_PER_UNIT
        act = bpy.data.actions.get(action_name_for(scope_key))
        if act:
            try:
                fcs = action_fcurves(act)
            except RuntimeError:
                fcs = ()
            for fc in fcs:
                for kp in fc.keyframe_points:
                    add(round(kp.co.x / scale, 4))
        for item in s.driven_items:
            for target_id, rel_path, p_idx, _is_con in custom_direct_resolved_targets(s, item):
                fc = find(target_id, rel_path, p_idx)
                if fc is not None and is_sdk(fc):
                    for kp in fc.keyframe_points:
                        add(round(kp.co.x, 4))
        return sorted(values)

    for item in s.driven_items:
        for target_id, rel_path, p_idx, _is_con in direct_resolved_targets(s, item, scope_key):
            fc = find(target_id, rel_path, p_idx)
            if fc is not None and is_sdk(fc):
                for kp in fc.keyframe_points:
                    add(round(kp.co.x, 4))
    return sorted(values)


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
        tuple((it.target_kind, it.obj.as_pointer() if it.obj else 0, it.bone, it.rna_path) for it in s.driven_items),
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
