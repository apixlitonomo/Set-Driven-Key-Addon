"""Set Driven Key — ops_driver."""

from bpy.props import (
    EnumProperty,
    IntProperty,
)
from bpy.types import Operator

from .const import KEY_EPS, _READ_ERRORS
from .paths import _is_bpy_data_path, _normalize_copied_path, extract_context_button_path, short_rna_label
from .drivers import auto_scrub_range, capture_driver_rest, resolve_driver_info, write_driver_value
from .query import get_all_keyed_values, invalidate_key_cache
from .ui import _can_pick_button_property



class SDK_OT_load_driver(Operator):
    bl_idname = "sdk.load_driver"
    bl_label = "Load Active"
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
        if s.driver_channel == "CUSTOM" and _is_bpy_data_path(s.driver_custom):
            s.driver_channel = "location:0"
        capture_driver_rest(s)
        auto_scrub_range(s)
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_clear_driver(Operator):
    bl_idname = "sdk.clear_driver"
    bl_label = "Clear Driver"
    bl_description = "Clear the current driver setup"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        s.driver_obj = None
        s.driver_bone = ""
        s.driver_custom = ""
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_paste_driver_path(Operator):
    bl_idname = "sdk.paste_driver_path"
    bl_label = "Paste Driver Path"
    bl_description = "Paste an RNA data path from the clipboard into the Driver Custom Path"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        raw = (context.window_manager.clipboard or "").strip()
        if not raw:
            self.report({"ERROR"}, "Clipboard is empty (right-click a property -> Copy Full Data Path)")
            return {"CANCELLED"}
        path = _normalize_copied_path(context, raw)
        s.driver_channel = "CUSTOM"
        s.driver_custom = path
        info = resolve_driver_info(s)
        if info.ok:
            capture_driver_rest(s)
            auto_scrub_range(s)
            self.report({"INFO"}, "Driver path set: %s" % short_rna_label(path))
        elif info.array_len > 0:
            self.report({"INFO"}, "Driver path pasted (pick array index [0..%d])" % (info.array_len - 1))
        else:
            self.report({"WARNING"}, "Pasted path (%s)" % (info.error or "check path"))
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_set_driver_index(Operator):
    bl_idname = "sdk.set_driver_index"
    bl_label = "Set Array Index"
    bl_description = "Append an array index [n] to the custom driver path"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=0, min=0)

    def execute(self, context):
        s = context.scene.sdk_settings
        s.driver_custom = "%s[%d]" % (s.driver_custom.strip(), self.index)
        capture_driver_rest(s)
        auto_scrub_range(s)
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_pick_driver_from_context(Operator):
    bl_idname = "sdk.pick_driver_from_context"
    bl_label = "Set as SDK Driver"
    bl_description = "Use this property as the Set Driven Key Driver"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _can_pick_button_property(context)

    def execute(self, context):
        path = extract_context_button_path(context)
        if not path:
            self.report({"WARNING"}, "Could not extract an RNA data path for this property")
            return {"CANCELLED"}
        s = context.scene.sdk_settings
        s.driver_channel = "CUSTOM"
        s.driver_custom = path
        info = resolve_driver_info(s)
        if info.ok:
            capture_driver_rest(s)
            auto_scrub_range(s)
            self.report({"INFO"}, "SDK Driver set: %s" % short_rna_label(path))
        elif info.array_len > 0:
            self.report({"INFO"}, "SDK Driver set (pick array index [0..%d] in Driven Key panel)" % (info.array_len - 1))
        else:
            self.report({"WARNING"}, "SDK Driver path set (%s)" % (info.error or "check path"))
        invalidate_key_cache()
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Main SDK Operators
# --------------------------------------------------------------------------

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
        info = resolve_driver_info(s)
        if not info.ok:
            self.report({"ERROR"}, info.error or "Load a valid driver first")
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
        info = resolve_driver_info(s)
        if not info.ok:
            self.report({"ERROR"}, info.error or "Load a valid driver first")
            return {"CANCELLED"}
        cur = info.value
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

        try:
            write_driver_value(s, target)
        except _READ_ERRORS as e:
            self.report({"ERROR"}, "Can't set driver value: %s" % e)
            return {"CANCELLED"}
        context.view_layer.update()
        self.report({"INFO"}, "Jumped to driver key: %.4f" % target)
        return {"FINISHED"}


class SDK_OT_scrub_range_auto(Operator):
    bl_idname = "sdk.scrub_range_auto"
    bl_label = "Auto Scrub Range"
    bl_description = "Set the scrub slider's Min / Max from the driver channel and its current value"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if resolve_driver_info(s).var_id is None:
            self.report({"ERROR"}, "Load a valid driver first")
            return {"CANCELLED"}
        auto_scrub_range(s)
        return {"FINISHED"}
