"""Set Driven Key — ops_driven."""

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
)
from bpy.types import Operator

from .paths import _normalize_copied_path, extract_context_button_path, inspect_rna_target, short_rna_label
from .drivers import driver_off_rest, transform_specs
from .constraints import capture, capture_custom_rest, reset_base, set_sdk_muted_for_settings
from .query import invalidate_key_cache
from .ui import _can_pick_button_property



# --------------------------------------------------------------------------
# Driver / Driven Picking & Path Operators
# --------------------------------------------------------------------------

def _add_rna_driven_item(s, rna_path):
    """Add or focus a Driven RNA_PATH item and capture its rest value."""
    norm = (rna_path or "").strip()
    for idx, it in enumerate(s.driven_items):
        if it.target_kind == "RNA_PATH" and it.rna_path.strip() == norm:
            s.driven_index = idx
            capture(it, s)
            return it, False
    item = s.driven_items.add()
    item.target_kind = "RNA_PATH"
    item.rna_path = norm
    s.driven_index = len(s.driven_items) - 1
    capture(item, s)
    invalidate_key_cache()
    return item, True


class SDK_OT_add_driven_path(Operator):
    bl_idname = "sdk.add_driven_path"
    bl_label = "Add Driven Path"
    bl_description = "Add an RNA data path target to the Driven list"
    bl_options = {"REGISTER", "UNDO"}

    from_clipboard: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        s = context.scene.sdk_settings
        path = ""
        if self.from_clipboard:
            raw = (context.window_manager.clipboard or "").strip()
            if not raw:
                self.report({"ERROR"}, "Clipboard is empty (right-click a property -> Copy Full Data Path)")
                return {"CANCELLED"}
            path = _normalize_copied_path(context, raw)
        item, created = _add_rna_driven_item(s, path)
        if self.from_clipboard:
            info = inspect_rna_target(None, item.rna_path)
            if info.ok:
                self.report({"INFO"}, "%s Driven path: %s" % ("Added" if created else "Focused", short_rna_label(item.rna_path)))
            elif info.array_len > 0:
                self.report({"INFO"}, "Added array path (pick index or click Split in the panel)")
            else:
                self.report({"WARNING"}, "Added path (%s)" % (info.error or "check path"))
        return {"FINISHED"}


class SDK_OT_paste_driven_item_path(Operator):
    bl_idname = "sdk.paste_driven_item_path"
    bl_label = "Paste Path"
    bl_description = "Paste an RNA data path from the clipboard into the selected Driven item"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if not (0 <= s.driven_index < len(s.driven_items)):
            return {"CANCELLED"}
        item = s.driven_items[s.driven_index]
        raw = (context.window_manager.clipboard or "").strip()
        if not raw:
            self.report({"ERROR"}, "Clipboard is empty")
            return {"CANCELLED"}
        item.target_kind = "RNA_PATH"
        item.rna_path = _normalize_copied_path(context, raw)
        capture(item, s)
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_set_driven_item_index(Operator):
    bl_idname = "sdk.set_driven_item_index"
    bl_label = "Set Array Index"
    bl_description = "Append an array index [n] to the selected Driven RNA path"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=0, min=0)

    def execute(self, context):
        s = context.scene.sdk_settings
        if not (0 <= s.driven_index < len(s.driven_items)):
            return {"CANCELLED"}
        item = s.driven_items[s.driven_index]
        if item.target_kind != "RNA_PATH":
            return {"CANCELLED"}
        item.rna_path = "%s[%d]" % (item.rna_path.strip(), self.index)
        capture(item, s)
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_split_driven_array(Operator):
    bl_idname = "sdk.split_driven_array"
    bl_label = "Split Array"
    bl_description = "Expand an array RNA path into separate [0]..[N-1] driven targets"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if not (0 <= s.driven_index < len(s.driven_items)):
            return {"CANCELLED"}
        item = s.driven_items[s.driven_index]
        if item.target_kind != "RNA_PATH":
            return {"CANCELLED"}
        base = item.rna_path.strip()
        info = inspect_rna_target(None, base)
        if info.array_len <= 0:
            return {"CANCELLED"}
        n = min(info.array_len, 16)
        item.rna_path = "%s[0]" % base
        capture(item, s)
        for i in range(1, n):
            _add_rna_driven_item(s, "%s[%d]" % (base, i))
        invalidate_key_cache()
        self.report({"INFO"}, "Expanded array into %d driven component(s)" % n)
        return {"FINISHED"}


class SDK_OT_capture_driven_item_rest(Operator):
    bl_idname = "sdk.capture_driven_item_rest"
    bl_label = "Capture Rest Value"
    bl_description = "Snapshot the current property value as this item's rest value"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if 0 <= s.driven_index < len(s.driven_items):
            capture(s.driven_items[s.driven_index], s)
        return {"FINISHED"}


class SDK_OT_paste_custom_channel(Operator):
    bl_idname = "sdk.paste_custom_channel"
    bl_label = "Paste Extra Path"
    bl_description = "Append an RNA data path from the clipboard to Extra Paths"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        raw = (context.window_manager.clipboard or "").strip()
        if not raw:
            self.report({"ERROR"}, "Clipboard is empty")
            return {"CANCELLED"}
        path = _normalize_copied_path(context, raw)
        existing = [p.strip() for p in s.ch_custom.split(";") if p.strip()]
        if path not in existing:
            existing.append(path)
        s.ch_custom = ";".join(existing)
        return {"FINISHED"}


class SDK_OT_clear_driven(Operator):
    bl_idname = "sdk.clear_driven"
    bl_label = "Clear Driven List"
    bl_description = "Remove all entries from the driven targets list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        s.driven_items.clear()
        s.driven_index = 0
        invalidate_key_cache()
        return {"FINISHED"}


class SDK_OT_pick_driven_from_context(Operator):
    bl_idname = "sdk.pick_driven_from_context"
    bl_label = "Add to SDK Driven"
    bl_description = "Add this property to the Set Driven Key Driven list"
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
        item, created = _add_rna_driven_item(s, path)
        info = inspect_rna_target(None, item.rna_path)
        if info.ok:
            self.report({"INFO"}, "%s SDK Driven: %s" % ("Added" if created else "Focused", short_rna_label(path)))
        elif info.array_len > 0:
            self.report({"INFO"}, "Added SDK Driven array (pick index or Split in Driven Key panel)")
        else:
            self.report({"WARNING"}, "Added SDK Driven path (%s)" % (info.error or "check path"))
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
            entries = [(pb.id_data, pb.name) for pb in context.selected_pose_bones]
        else:
            entries = [(o, "") for o in context.selected_objects]
        if not entries:
            self.report({"ERROR"}, "Nothing selected")
            return {"CANCELLED"}
        if not self.append:
            s.driven_items.clear()
        existing = {(i.obj.as_pointer() if i.obj else 0, i.bone)
                    for i in s.driven_items if i.target_kind == "OBJECT"}
        for obj, bone in entries:
            key = (obj.as_pointer(), bone)
            if key in existing:
                continue
            existing.add(key)
            item = s.driven_items.add()
            item.target_kind = "OBJECT"
            item.obj = obj
            item.bone = bone
            capture(item, s)
        s.driven_index = max(0, len(s.driven_items) - 1)
        set_sdk_muted_for_settings(s, not s.live_preview)
        invalidate_key_cache()
        if s.mode == "DIRECT" and driver_off_rest(s):
            self.report({"WARNING"}, "Driver is not at its rest value: base values captured for Direct mode "
                                     "include its effect. Use 'Driver to Rest' and load again for a clean base")
        return {"FINISHED"}


class SDK_OT_load_shape_keys(Operator):
    bl_idname = "sdk.load_shape_keys"
    bl_label = "Load Mesh Shape Keys"
    bl_description = "Add all shape keys of the active mesh to Extra Paths"
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

        ptr = obj.as_pointer()
        item = next(
            (i for i in s.driven_items
             if i.target_kind == "OBJECT" and i.obj and i.obj.as_pointer() == ptr and not i.bone),
            None,
        )
        added = item is None
        if added:
            item = s.driven_items.add()
            item.target_kind = "OBJECT"
            item.obj = obj
            item.bone = ""
            capture(item, s)
        else:
            capture_custom_rest(item, s, only_missing=True)
        invalidate_key_cache()
        self.report({"INFO"}, "Added %d shape key(s) to Extra Paths%s"
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
    bl_description = "Re-capture current values of driven items as the rest / additive base"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        for item in s.driven_items:
            capture(item, s)
        return {"FINISHED"}


class SDK_OT_reset_base(Operator):
    bl_idname = "sdk.reset_base"
    bl_label = "Reset to Base"
    bl_description = "Put enabled driven object/bone channels back to captured base values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        specs = transform_specs(s)
        for item in s.driven_items:
            if item.target_kind == "OBJECT":
                reset_base(item, specs)
        context.view_layer.update()
        return {"FINISHED"}
