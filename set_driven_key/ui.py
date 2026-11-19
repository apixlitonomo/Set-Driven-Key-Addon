"""Set Driven Key — ui."""

import bpy
from bpy.types import Panel, UIList

from .const import _READ_ERRORS
from .paths import _is_bpy_data_path, inspect_rna_target, resolve_bpy_data_prefix, short_rna_label
from .drivers import has_bone_targets, has_object_targets, resolve_driver_info
from .query import cached_keyed_values
from .props import SDK_ConfigItem, SDK_DrivenItem, SDK_Settings



class SDK_UL_configs(UIList):
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
        if self.filter_name:
            flt_flags = [
                self.bitflag_filter_item
                if (self.filter_name.lower() in item.name.lower() or self.filter_name.lower() in item.config_layer_tag.lower())
                else 0
                for item in items
            ]
        return flt_flags, []


def _can_pick_button_property(context):
    ptr = getattr(context, "button_pointer", None)
    if isinstance(ptr, (SDK_Settings, SDK_DrivenItem, SDK_ConfigItem)):
        return False
    prop = getattr(context, "button_prop", None)
    if prop is not None and prop.type not in {"FLOAT", "INT", "BOOLEAN"}:
        return False
    if bpy.ops.ui.copy_data_path_button.poll():
        return True
    return getattr(context, "property", None) is not None


def _draw_button_context_menu(self, context):
    if _can_pick_button_property(context):
        layout = self.layout
        layout.separator()
        layout.operator("sdk.pick_driver_from_context", icon="DRIVER")
        layout.operator("sdk.pick_driven_from_context", icon="LINKED")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

class SDK_UL_driven(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item.target_kind == "RNA_PATH":
            label = short_rna_label(item.rna_path)
            if not item.rna_path.strip():
                layout.label(text="<empty RNA path>", icon="RNA")
            elif _is_bpy_data_path(item.rna_path):
                try:
                    resolve_bpy_data_prefix(item.rna_path)
                    layout.label(text=label, icon="RNA")
                except _READ_ERRORS:
                    layout.label(text=label, icon="ERROR")
            else:
                layout.label(text=label, icon="ERROR")
            return

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
        drv_info = resolve_driver_info(s)

        # ---- Driver -------------------------------------------------------
        box = layout.box()
        hdr = box.row(align=True)
        hdr.label(text="Driver", icon="DRIVER")
        if drv_info.ok:
            hdr.label(text=drv_info.label, icon="BONE_DATA" if drv_info.bone else (
                "OBJECT_DATA" if isinstance(drv_info.var_id, bpy.types.Object) else "RNA"
            ))
        elif s.driver_obj or (s.driver_channel == "CUSTOM" and s.driver_custom.strip()):
            hdr.label(text=drv_info.label, icon="ERROR")

        row = box.row(align=True)
        row.operator("sdk.load_driver", text="Load Active", icon="EYEDROPPER")
        row.operator("sdk.paste_driver_path", text="Paste Path", icon="PASTEDOWN")
        if s.driver_obj or (s.driver_channel == "CUSTOM" and s.driver_custom.strip()):
            row.operator("sdk.clear_driver", text="", icon="X")

        box.prop(s, "driver_channel", text="")
        if s.driver_channel == "CUSTOM":
            row = box.row(align=True)
            row.prop(s, "driver_custom", text="")
            row.operator("sdk.paste_driver_path", text="", icon="PASTEDOWN")

            if drv_info.array_len > 0:
                sub = box.box()
                sub.label(text="Array property — pick index:", icon="INFO")
                arow = sub.row(align=True)
                for idx in range(min(drv_info.array_len, 6)):
                    arow.operator("sdk.set_driver_index", text="[%d]" % idx).index = idx
            elif not drv_info.ok and s.driver_custom.strip():
                err = box.row()
                err.alert = True
                err.label(text=drv_info.error, icon="ERROR")

        if drv_info.ok:
            box.prop(s, "driver_scrub_t", text="Scrub", slider=True)
            row = box.row(align=True)
            row.prop(s, "driver_scrub", text="Value")
            row.prop(s, "scrub_min", text="Min")
            row.prop(s, "scrub_max", text="Max")
            row.operator("sdk.scrub_range_auto", text="", icon="AUTO")
            row = box.row(align=True)
            row.prop(s, "driver_rest", text="Rest")
            row.operator("sdk.capture_driver_rest", text="", icon="PINNED")
            row.operator("sdk.driver_to_rest", text="", icon="LOOP_BACK")

            keys = cached_keyed_values(s, drv_info.scope_key)
            row = box.row(align=True)
            row.operator("sdk.jump_key", text="Prev Key", icon="PREV_KEYFRAME").direction = "PREV"
            row.operator("sdk.jump_key", text="Next Key", icon="NEXT_KEYFRAME").direction = "NEXT"
            if keys:
                box.label(text="Keys: %s" % ", ".join("%.2f" % k for k in keys[:6]), icon="KEYFRAME")
            else:
                box.label(text="No keys stored on channels yet", icon="INFO")
        elif s.driver_channel != "CUSTOM":
            box.label(text="Pick an object/bone, choose Custom Path, or right-click any property", icon="INFO")

        # ---- Driven -------------------------------------------------------
        box = layout.box()
        hdr = box.row(align=True)
        hdr.label(text="Driven (%d)" % len(s.driven_items), icon="LINKED")

        row = box.row(align=True)
        row.operator("sdk.load_driven", text="Load Sel", icon="EYEDROPPER").append = False
        row.operator("sdk.load_driven", text="+ Sel", icon="ADD").append = True
        row.operator("sdk.add_driven_path", text="Paste Path", icon="PASTEDOWN").from_clipboard = True

        row = box.row()
        row.template_list("SDK_UL_driven", "", s, "driven_items", s, "driven_index", rows=3)
        col = row.column(align=True)
        col.operator("sdk.add_driven_path", text="", icon="ADD").from_clipboard = False
        col.operator("sdk.remove_driven", text="", icon="REMOVE")
        if len(s.driven_items):
            col.operator("sdk.clear_driven", text="", icon="TRASH")

        # Active RNA_PATH item editor
        if 0 <= s.driven_index < len(s.driven_items):
            active_it = s.driven_items[s.driven_index]
            if active_it.target_kind == "RNA_PATH":
                rbox = box.box()
                rrow = rbox.row(align=True)
                rrow.prop(active_it, "rna_path", text="")
                rrow.operator("sdk.paste_driven_item_path", text="", icon="PASTEDOWN")
                t_info = inspect_rna_target(None, active_it.rna_path)
                if t_info.ok:
                    vrow = rbox.row(align=True)
                    rest_txt = ("%.3f" % active_it.rest_rna) if active_it.has_rest_rna else "auto"
                    vrow.label(text="Val: %.3f  |  Rest: %s" % (t_info.value, rest_txt), icon="CHECKMARK")
                    vrow.operator("sdk.capture_driven_item_rest", text="", icon="PINNED")
                elif t_info.array_len > 0:
                    rbox.label(text="Array (len %d) — pick index or split:" % t_info.array_len, icon="INFO")
                    arow = rbox.row(align=True)
                    for idx in range(min(t_info.array_len, 4)):
                        arow.operator("sdk.set_driven_item_index", text="[%d]" % idx).index = idx
                    arow.operator("sdk.split_driven_array", text="Split All")
                else:
                    err = rbox.row()
                    err.alert = True
                    err.label(text=t_info.error or "Invalid RNA path", icon="ERROR")

        # Object / Bone channels (shown only when at least one OBJECT target is loaded)
        if has_object_targets(s):
            cbox = box.box()
            row = cbox.row(align=True)
            row.label(text="Object / Bone Channels:")
            row.operator("sdk.channel_preset", text="All").action = "ALL"
            row.operator("sdk.channel_preset", text="Loc").action = "LOC"
            row.operator("sdk.channel_preset", text="Rot").action = "ROT"
            row.operator("sdk.channel_preset", text="Scale").action = "SCALE"
            row.operator("sdk.channel_preset", text="Clear").action = "CLEAR"

            col = cbox.column(align=True)
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

            erow = cbox.row(align=True)
            erow.prop(s, "ch_custom", text="Extra Paths")
            erow.operator("sdk.paste_custom_channel", text="", icon="PASTEDOWN")
            cbox.operator("sdk.load_shape_keys", text="Add Mesh Shape Keys", icon="SHAPEKEY_DATA")

        # ---- Mode & Keying ------------------------------------------------
        kbox = layout.box()
        kbox.prop(s, "mode")
        if has_object_targets(s):
            if s.mode in ("ADDITIVE", "ACTION"):
                row = kbox.row(align=True)
                row.operator("sdk.capture_base", icon="PINNED")
                row.operator("sdk.reset_base", icon="LOOP_BACK")
                kbox.prop(s, "auto_reset")
            if s.mode == "ACTION":
                kbox.operator("sdk.action_refresh", icon="FILE_REFRESH")
        elif len(s.driven_items):
            kbox.label(text="Custom RNA paths use Direct drivers", icon="INFO")

        kbox.prop(s, "auto_rest_key")

        row = kbox.row(align=True)
        row.scale_y = 1.5
        row.operator("sdk.key", icon="KEY_HLT")
        row = kbox.row(align=True)
        row.operator("sdk.delete_key", text="Delete Key", icon="KEY_DEHLT")
        row.operator("sdk.delete_curves", text="Delete Curves", icon="TRASH")
        if has_bone_targets(s):
            kbox.operator("sdk.mirror_keys", text="Mirror .L / .R Poses", icon="MOD_MIRROR")
        kbox.prop(
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


class SDK_PT_configs(Panel):
    bl_label = "Configurations"
    bl_idname = "SDK_PT_configs"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Driven Key"
    bl_parent_id = "SDK_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        s = context.scene.sdk_settings
        layout = self.layout

        row = layout.row(align=True)
        row.template_list("SDK_UL_configs", "", s, "configs", s, "config_index", rows=3)
        col = row.column(align=True)
        col.operator("sdk.config_add", text="", icon="ADD")
        col.operator("sdk.config_remove", text="", icon="REMOVE")
        col.operator("sdk.config_load", text="", icon="IMPORT")

        row = layout.row(align=True)
        row.prop(s, "config_layer_tag", text="Layer Tag")
        row.operator("sdk.config_load", text="Load", icon="CHECKMARK")

        row = layout.row(align=True)
        row.operator("sdk.config_export", text="Export JSON", icon="EXPORT")
        row.operator("sdk.config_import", text="Import JSON", icon="FILE_FOLDER")


def _context_menu_type():
    return getattr(bpy.types, "UI_MT_button_context_menu", None) or getattr(bpy.types, "WM_MT_button_context", None)
