"""Set Driven Key — ops_keys."""

from types import SimpleNamespace

import bpy
from bpy.types import Operator

from .const import ACTION_FRAMES_PER_UNIT, DIRECT_ATTR, KEY_EPS, _RNA_ERRORS, _WARNINGS
from .util import _report_problems, safe_call
from .paths import con_data_path, full_path, inspect_rna_target, short_rna_label
from .drivers import _remove_driver_target, custom_specs, enabled_channels, find_driver_fcurve, has_object_targets, is_sdk_fcurve, owner_of, quat_specs, resolve_driver_info, transform_specs
from .dummy import cleanup_dummy_target
from .constraints import custom_direct_resolved_targets, direct_resolved_targets, find_sdk_constraint, is_sdk_transform_constraint, key_additive, key_direct, key_direct_rna, reset_base, set_sdk_muted_for_settings
from .action import action_constraint_name_for, action_fcurves, action_name_for, action_rotation_skip, get_action_for_driver, key_action_mode, remove_eval_time_driver, update_action_constraint_ranges
from .query import invalidate_key_cache
from .mirror import flip_name, mirror_action, mirror_additive, mirror_direct, mirror_signs



class SDK_OT_action_refresh(Operator):
    bl_idname = "sdk.action_refresh"
    bl_label = "Refresh Action Range"
    bl_description = "Re-sync the Action constraint ranges with the keys currently in the SDK Action"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        del _WARNINGS[:]
        drv_info = resolve_driver_info(s)
        if drv_info.var_id is None:
            self.report({"ERROR"}, drv_info.error or "Load a valid driver first")
            return {"CANCELLED"}
        action = bpy.data.actions.get(action_name_for(drv_info.scope_key))
        if action is None:
            self.report({"ERROR"}, "No Action exists for the current driver; key something first")
            return {"CANCELLED"}
        update_action_constraint_ranges(action, s, drv_info)
        invalidate_key_cache()
        context.view_layer.update()
        _report_problems(self, list(_WARNINGS))
        self.report({"INFO"}, "Refreshed the range of '%s'" % action.name)
        return {"FINISHED"}


class SDK_OT_mirror_keys(Operator):
    bl_idname = "sdk.mirror_keys"
    bl_label = "Mirror / Flip to Symmetrical Bones"
    bl_description = "Copy and mirror SDK drivers and curves to counterpart bones (e.g. .L <-> .R)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        del _WARNINGS[:]
        drv_info = resolve_driver_info(s)
        if not drv_info.ok:
            self.report({"ERROR"}, drv_info.error or "Load a valid driver first")
            return {"CANCELLED"}

        scope_key = drv_info.scope_key
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
            if item.target_kind != "OBJECT":
                continue
            obj = item.obj
            if not item.bone or obj is None or obj.pose is None:
                continue
            opp = flip_name(item.bone)
            if not opp or opp not in obj.pose.bones:
                unpaired += 1
                continue
            pair = (obj.as_pointer(), frozenset((item.bone, opp)))
            if pair in done_pairs:
                continue
            done_pairs.add(pair)

            dst = SimpleNamespace(target_kind="OBJECT", obj=obj, bone=opp)
            signs, exact = mirror_signs(obj, item.bone, opp)
            if not exact:
                problems.append("'%s' / '%s': rolls don't allow exact per-axis mirroring; "
                                "used the standard X-mirror signs" % (item.bone, opp))
            try:
                if s.mode == "DIRECT":
                    c, k = mirror_direct(s, item, dst, signs, drv_info, problems)
                elif s.mode == "ADDITIVE":
                    c, k = mirror_additive(s, item, dst, signs, drv_info, problems)
                else:
                    c, k = mirror_action(s, item, dst, signs, drv_info, action, problems)
            except RuntimeError as e:
                problems.append(str(e))
                continue
            curves += c
            cons += k
            if c:
                dst_items.append(dst)

        if action is not None and dst_items:
            update_action_constraint_ranges(action, s, drv_info, dst_items)
        set_sdk_muted_for_settings(s, not s.live_preview)
        invalidate_key_cache()
        context.view_layer.update()

        problems.extend(_WARNINGS)
        _report_problems(self, problems)
        if not curves:
            self.report({"WARNING"}, "Nothing mirrored: no keyed bone channels with a .L/.R (or _L/_R, .left/.right) "
                                     "counterpart in %s mode" % s.mode.title())
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
        drv_info = resolve_driver_info(s)
        if not drv_info.ok:
            self.report({"ERROR"}, drv_info.error or "Load a valid driver first")
            return {"CANCELLED"}

        x = drv_info.value
        scope_key = drv_info.scope_key

        if not len(s.driven_items):
            self.report({"ERROR"}, "Add at least one Driven object or RNA path first")
            return {"CANCELLED"}

        tspecs, qspecs, cspecs = transform_specs(s), quat_specs(s), custom_specs(s)
        has_rna_items = any(it.target_kind == "RNA_PATH" for it in s.driven_items)
        has_obj_items = has_object_targets(s)

        if has_obj_items and not (tspecs or qspecs or cspecs) and not has_rna_items:
            self.report({"ERROR"}, "Enable at least one driven channel (or add an RNA Path / Extra Path)")
            return {"CANCELLED"}

        mode = s.mode
        problems = []
        if mode == "ADDITIVE" and qspecs:
            problems.append("Quaternion channels are Direct/Action-mode only")

        keyed = 0
        needs_action = (mode == "ACTION" and has_obj_items and (tspecs or qspecs))
        action = get_action_for_driver(scope_key) if needs_action else None

        for item in s.driven_items:
            if item.target_kind == "RNA_PATH":
                target_info = inspect_rna_target(None, item.rna_path)
                msg = safe_call(key_direct_rna, short_rna_label(item.rna_path),
                                s, item, target_info, drv_info, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1
                continue

            obj = item.obj
            if obj is None:
                continue
            if owner_of(item) is None:
                problems.append("'%s' not found" % (item.bone or obj.name))
                continue

            if not (tspecs or qspecs or cspecs):
                problems.append("No driven channels enabled for '%s'" % (item.bone or obj.name))
                continue

            done = []
            for kind, axis in tspecs:
                label = "%s %s%s" % (item.bone or obj.name, kind, "XYZ"[axis])
                if mode == "ACTION" and kind == "rot":
                    skip = action_rotation_skip(item, True, bool(qspecs))
                    if skip is not None:
                        if skip:
                            problems.append(skip)
                        continue
                if mode == "ADDITIVE":
                    msg = safe_call(key_additive, label, s, item, kind, axis, drv_info, scope_key, x)
                elif mode == "DIRECT":
                    msg = safe_call(key_direct, label, s, item, DIRECT_ATTR[kind], axis, drv_info, x)
                else:
                    msg = safe_call(key_action_mode, label, s, item, DIRECT_ATTR[kind], axis,
                                    action, scope_key, drv_info, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1
                    done.append((kind, axis))

            if mode != "ADDITIVE":
                for i in qspecs:
                    label = "%s quat %s" % (item.bone or obj.name, "WXYZ"[i])
                    if mode == "ACTION":
                        skip = action_rotation_skip(item, False, any(k == "rot" for k, _a in tspecs))
                        if skip is not None:
                            if skip:
                                problems.append(skip)
                            continue
                    if mode == "DIRECT":
                        msg = safe_call(key_direct, label, s, item, "rotation_quaternion", i, drv_info, x)
                    else:
                        msg = safe_call(key_action_mode, label, s, item, "rotation_quaternion", i,
                                        action, scope_key, drv_info, x)
                    if msg:
                        problems.append(msg)
                    else:
                        keyed += 1
                        if mode == "ACTION" and ("rot", 0) not in done:
                            done.append(("rot", 0))

            for rel in cspecs:
                label = "%s %s" % (item.bone or obj.name, rel)
                target_info = inspect_rna_target(obj, rel, bone=item.bone)
                msg = safe_call(key_direct_rna, label, s, item, target_info, drv_info, x, raw_key=rel)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1

            if (mode == "ADDITIVE" or mode == "ACTION") and s.auto_reset and done:
                reset_base(item, done)

        if mode == "ACTION" and action:
            update_action_constraint_ranges(action, s, drv_info)

        set_sdk_muted_for_settings(s, not s.live_preview)
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
        drv_info = resolve_driver_info(s)
        if not drv_info.ok:
            self.report({"ERROR"}, drv_info.error or "Load a valid driver first")
            return {"CANCELLED"}
        x = drv_info.value
        scope_key = drv_info.scope_key

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
                wanted = set()
                for item in s.driven_items:
                    if getattr(item, "target_kind", "OBJECT") != "OBJECT" or item.obj is None:
                        continue
                    for attr, ax in enabled_channels(s):
                        wanted.add((full_path(item.bone, attr), ax))
                for fc in list(fcs):
                    if (fc.data_path, fc.array_index) not in wanted:
                        continue
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - frame) < KEY_EPS * ACTION_FRAMES_PER_UNIT:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                update_action_constraint_ranges(act, s, drv_info)
            for item in s.driven_items:
                for target_id, rel_path, p_idx, _is_con in custom_direct_resolved_targets(s, item):
                    fc = find_driver_fcurve(target_id, rel_path, p_idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - x) < KEY_EPS:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                            break
                    if not len(fc.keyframe_points):
                        _remove_driver_target(target_id, rel_path, p_idx, _is_con)
        else:
            for item in s.driven_items:
                for target_id, rel_path, p_idx, _is_con in direct_resolved_targets(s, item, scope_key):
                    fc = find_driver_fcurve(target_id, rel_path, p_idx)
                    if fc is None or not is_sdk_fcurve(fc):
                        continue
                    for kp in list(fc.keyframe_points):
                        if abs(kp.co.x - x) < KEY_EPS:
                            fc.keyframe_points.remove(kp)
                            fc.update()
                            removed += 1
                            break
                    if not len(fc.keyframe_points):
                        _remove_driver_target(target_id, rel_path, p_idx, _is_con)
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
        drv_info = resolve_driver_info(s)
        scope_key = drv_info.scope_key
        removed = 0
        orphans = 0

        if s.mode == "ACTION":
            act = bpy.data.actions.get(action_name_for(scope_key))
            con_name = action_constraint_name_for(scope_key)
            for item in s.driven_items:
                if item.target_kind == "OBJECT":
                    owner = owner_of(item)
                    con = find_sdk_constraint(owner, con_name, "ACTION") if owner else None
                    if con is not None:
                        remove_eval_time_driver(item, con.name)
                        owner.constraints.remove(con)
                        removed += 1
                for target_id, rel_path, p_idx, is_con in custom_direct_resolved_targets(s, item):
                    if _remove_driver_target(target_id, rel_path, p_idx, is_con):
                        removed += 1
            if act and act.users <= 1:
                bpy.data.actions.remove(act)
        else:
            for item in s.driven_items:
                for target_id, rel_path, p_idx, is_con in direct_resolved_targets(s, item, scope_key):
                    if _remove_driver_target(target_id, rel_path, p_idx, is_con):
                        removed += 1

                if item.target_kind != "OBJECT" or item.obj is None:
                    continue
                obj = item.obj
                ad = obj.animation_data
                if ad:
                    for fc in list(ad.drivers):
                        if not is_sdk_fcurve(fc) or ".constraints[" not in fc.data_path:
                            continue
                        try:
                            obj.path_resolve(fc.data_path)
                        except _RNA_ERRORS:
                            try:
                                obj.driver_remove(fc.data_path, fc.array_index)
                            except _RNA_ERRORS:
                                continue
                            orphans += 1

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
        msg = "Removed %d driven relationship(s)" % removed
        if orphans:
            msg += " and %d orphaned driver(s)" % orphans
        self.report({"INFO"}, msg)
        return {"FINISHED"}
