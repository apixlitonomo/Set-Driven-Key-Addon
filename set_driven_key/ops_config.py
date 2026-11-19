"""Set Driven Key — ops_config."""

import json
import re
import sys

from bpy.props import (
    StringProperty,
)
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .paths import _is_bpy_data_path, inspect_rna_target, short_rna_label
from .constraints import capture, set_sdk_muted_for_settings
from .drivers import DRIVER_ITEMS, driver_off_rest
from .query import invalidate_key_cache
from .config import _bools, _config_to_dict, _ref_label, as_dict, obj_ref, parse_driven_data, resolve_config_object

_VALID_MODES = ("ADDITIVE", "DIRECT", "ACTION")
_VALID_CHANNELS = frozenset(item[0] for item in DRIVER_ITEMS)



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
        uses_abs = (s.driver_channel == "CUSTOM" and _is_bpy_data_path(s.driver_custom))
        cfg.driver_obj = None if uses_abs else s.driver_obj
        cfg.driver_ref = "{}" if uses_abs else json.dumps(obj_ref(s.driver_obj))
        cfg.driver_bone = "" if uses_abs else s.driver_bone
        cfg.driver_channel = s.driver_channel
        cfg.driver_custom = s.driver_custom
        cfg.driver_rest = s.driver_rest
        cfg.mode = s.mode
        cfg.config_layer_tag = s.config_layer_tag
        cfg.ch_loc = s.ch_loc
        cfg.ch_rot = s.ch_rot
        cfg.ch_quat = s.ch_quat
        cfg.ch_scale = s.ch_scale
        cfg.ch_custom = s.ch_custom

        entries = []
        skipped = 0
        cfg.driven_refs.clear()
        for it in s.driven_items:
            if it.target_kind == "RNA_PATH":
                if not it.rna_path.strip():
                    skipped += 1
                    continue
                entries.append({"kind": "RNA_PATH", "rna_path": it.rna_path, "obj": {}, "bone": ""})
                cfg.driven_refs.add().obj = None
            else:
                if it.obj is None:
                    skipped += 1
                    continue
                entries.append({"kind": "OBJECT", "obj": obj_ref(it.obj), "bone": it.bone, "rna_path": ""})
                cfg.driven_refs.add().obj = it.obj
        cfg.driven_data = json.dumps(entries)

        s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Saved configuration '%s'" % cfg.name)
        if skipped:
            self.report({"WARNING"}, "%d invalid or missing driven item(s) were not saved" % skipped)
        return {"FINISHED"}

    def invoke(self, context, event):
        s = context.scene.sdk_settings
        if s.driver_channel == "CUSTOM" and s.driver_custom.strip():
            self.name = re.sub(r"[^A-Za-z0-9_]", "_", short_rna_label(s.driver_custom, 24)).strip("_") or "SDK_Config"
        else:
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
            drv_ref = as_dict(json.loads(cfg.driver_ref or "{}"))
        except ValueError:
            drv_ref = {}
        mode = cfg.mode if cfg.mode in _VALID_MODES else "ADDITIVE"
        channel = cfg.driver_channel if cfg.driver_channel in _VALID_CHANNELS else "location:0"
        if mode != cfg.mode or channel != cfg.driver_channel:
            notes.append("invalid mode/driver channel in '%s' replaced by defaults" % cfg.name)
        drv_obj, how = resolve_config_object(cfg.driver_obj, drv_ref)
        uses_abs = (cfg.driver_channel == "CUSTOM" and _is_bpy_data_path(cfg.driver_custom))
        if not uses_abs:
            if drv_obj is None and (cfg.driver_obj is not None or drv_ref):
                missing.append("driver object '%s'" % (drv_ref.get("name") or "?"))
            elif how in ("name", "data"):
                notes.append("driver object found by %s" % how)

        s.driver_obj = drv_obj
        s.driver_bone = cfg.driver_bone
        if drv_obj is not None and cfg.driver_bone and (
                drv_obj.pose is None or cfg.driver_bone not in drv_obj.pose.bones):
            missing.append("driver bone '%s'" % cfg.driver_bone)
        s.driver_channel = channel
        s.driver_custom = cfg.driver_custom
        s.driver_rest = cfg.driver_rest
        s.mode = mode
        s.config_layer_tag = cfg.config_layer_tag
        s.ch_loc = cfg.ch_loc
        s.ch_rot = cfg.ch_rot
        s.ch_quat = cfg.ch_quat
        s.ch_scale = cfg.ch_scale
        s.ch_custom = cfg.ch_custom

        try:
            entries = parse_driven_data(cfg.driven_data)
        except (ValueError, TypeError) as e:
            entries = None
            self.report({"WARNING"}, "Could not read driven targets of '%s': %s" % (cfg.name, e))
        if entries is not None:
            s.driven_items.clear()
            for i, entry in enumerate(entries):
                if entry["kind"] == "RNA_PATH":
                    item = s.driven_items.add()
                    item.target_kind = "RNA_PATH"
                    item.rna_path = entry["rna_path"]
                    info = inspect_rna_target(None, item.rna_path)
                    if not info.ok:
                        missing.append(short_rna_label(item.rna_path))
                    else:
                        capture(item, s)
                    continue

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
                item.target_kind = "OBJECT"
                item.obj = obj
                item.bone = bone
                capture(item, s)

        set_sdk_muted_for_settings(s, not s.live_preview)
        invalidate_key_cache()
        self.report({"INFO"}, "Loaded configuration '%s'" % cfg.name)
        if s.mode == "DIRECT" and driver_off_rest(s):
            self.report({"WARNING"}, "Driver is not at its rest value: base values captured for Direct mode "
                                     "include its effect. Use 'Driver to Rest' and re-load for a clean base")
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


class SDK_OT_config_export(Operator, ExportHelper):
    bl_idname = "sdk.config_export"
    bl_label = "Export Configs"
    bl_description = "Save all configurations to a JSON file"
    bl_options = {"REGISTER"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        s = context.scene.sdk_settings
        payload = {
            "addon": "Set Driven Key",
            "version": list(getattr(sys.modules.get(__package__), "bl_info", {}).get("version", ())),
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
        if not isinstance(items, list):
            self.report({"ERROR"}, "Could not read configurations: 'configs' is not a list")
            return {"CANCELLED"}
        for c in items:
            n_before = len(s.configs)
            try:
                drv = as_dict(c.get("driver", {}))
                ch = as_dict(c.get("channels", {}))
                cfg = s.configs.add()
                cfg.name = c.get("name", "Imported_SDK_Config")
                cfg.config_layer_tag = c.get("layer_tag", "")
                mode = c.get("mode", "ADDITIVE")
                cfg.mode = mode if mode in _VALID_MODES else "ADDITIVE"
                drv_ref = as_dict(drv.get("obj"))
                cfg.driver_ref = json.dumps(drv_ref)
                obj, _how = resolve_config_object(None, drv_ref)
                cfg.driver_obj = obj
                uses_abs = (drv.get("channel") == "CUSTOM" and _is_bpy_data_path(drv.get("custom", "")))
                if not uses_abs and obj is None and drv_ref:
                    missing.append("driver object '%s'" % (drv_ref.get("name") or "?"))
                cfg.driver_bone = drv.get("bone", "")
                channel = drv.get("channel", "location:0")
                cfg.driver_channel = channel if channel in _VALID_CHANNELS else "location:0"
                cfg.driver_custom = drv.get("custom", "")
                cfg.driver_rest = float(drv.get("rest", 0.0))
                cfg.ch_loc = _bools(ch.get("loc"), 3)
                cfg.ch_rot = _bools(ch.get("rot"), 3)
                cfg.ch_quat = _bools(ch.get("quat"), 4)
                cfg.ch_scale = _bools(ch.get("scale"), 3)
                cfg.ch_custom = ch.get("custom", "")
                entries = []
                for d in c.get("driven", []):
                    kind = d.get("kind") or ("RNA_PATH" if d.get("rna_path") else "OBJECT")
                    if kind == "RNA_PATH":
                        rpath = d.get("rna_path", "")
                        entries.append({"kind": "RNA_PATH", "rna_path": rpath, "obj": {}, "bone": ""})
                        cfg.driven_refs.add().obj = None
                        if not inspect_rna_target(None, rpath).ok:
                            missing.append(short_rna_label(rpath))
                    else:
                        ref = as_dict(d.get("obj"))
                        entries.append({"kind": "OBJECT", "obj": ref, "bone": d.get("bone", ""), "rna_path": ""})
                        d_obj, _how = resolve_config_object(None, ref)
                        cfg.driven_refs.add().obj = d_obj
                        if d_obj is None:
                            missing.append(_ref_label(ref, d.get("bone", "")))
                cfg.driven_data = json.dumps(entries)
                added += 1
            except (AttributeError, TypeError, ValueError, KeyError) as e:
                while len(s.configs) > n_before:  # drop the half-built entry
                    s.configs.remove(len(s.configs) - 1)
                self.report({"WARNING"}, "Skipped a malformed configuration: %s" % e)
        if added:
            s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Imported %d configuration(s)" % added)
        if missing:
            self.report({"WARNING"}, "Not found in this file (kept by path/name): %s"
                        % (", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")))
        return {"FINISHED"} if added else {"CANCELLED"}
