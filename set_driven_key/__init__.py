"""Set Driven Key package."""

bl_info = {
    "name": "Set Driven Key",
    "author": "OceanMan, CloudKing, Apixelitonomo, U.F.Olive",
    "version": (1, 7, 2),
    "blender": (3, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Driven Key & Property Right-Click Menu",
    "description": (
        "Professional Set Driven Key for Blender with Right-Click Picking, "
        "Configuration Manager, Multi-Axis Additive, Direct, and Action Constraint modes"
    ),
    "category": "Animation",
    "doc_url": "https://github.com/apixlitonomo/Set-Driven-Key-Addon",
    "tracker_url": "https://github.com/apixlitonomo/Set-Driven-Key-Addon/issues",
}

import bpy
from bpy.props import PointerProperty

from .props import SDK_ConfigItem, SDK_ConfigObjRef, SDK_DrivenItem, SDK_Settings
from .ops_config import (
    SDK_OT_config_add,
    SDK_OT_config_export,
    SDK_OT_config_import,
    SDK_OT_config_load,
    SDK_OT_config_remove,
)
from .ops_driver import (
    SDK_OT_capture_driver_rest,
    SDK_OT_clear_driver,
    SDK_OT_driver_to_rest,
    SDK_OT_jump_key,
    SDK_OT_load_driver,
    SDK_OT_paste_driver_path,
    SDK_OT_pick_driver_from_context,
    SDK_OT_scrub_range_auto,
    SDK_OT_set_driver_index,
)
from .ops_driven import (
    SDK_OT_add_driven_path,
    SDK_OT_capture_base,
    SDK_OT_capture_driven_item_rest,
    SDK_OT_channel_preset,
    SDK_OT_clear_driven,
    SDK_OT_load_driven,
    SDK_OT_load_shape_keys,
    SDK_OT_paste_custom_channel,
    SDK_OT_paste_driven_item_path,
    SDK_OT_pick_driven_from_context,
    SDK_OT_remove_driven,
    SDK_OT_reset_base,
    SDK_OT_set_driven_item_index,
    SDK_OT_split_driven_array,
)
from .ops_keys import (
    SDK_OT_action_refresh,
    SDK_OT_delete_curves,
    SDK_OT_delete_key,
    SDK_OT_key,
    SDK_OT_mirror_keys,
)
from .ui import (
    SDK_PT_configs,
    SDK_PT_main,
    SDK_PT_options,
    SDK_UL_configs,
    SDK_UL_driven,
    _context_menu_type,
    _draw_button_context_menu,
)
from .query import _on_history_change
from .dummy import cleanup_dummy_target

# Names other scripts and the test suite import from this package.
from .action import action_fcurves, action_name_for, find_action_for_driver
from .constraints import capture
from .drivers import auto_scrub_range, capture_driver_rest, resolve_driver_info, write_driver_value
from .ops_driven import _add_rna_driven_item

classes = (
    SDK_ConfigObjRef,
    SDK_ConfigItem,
    SDK_DrivenItem,
    SDK_Settings,
    SDK_OT_config_add,
    SDK_OT_config_load,
    SDK_OT_config_remove,
    SDK_OT_config_export,
    SDK_OT_config_import,
    SDK_OT_load_driver,
    SDK_OT_clear_driver,
    SDK_OT_paste_driver_path,
    SDK_OT_set_driver_index,
    SDK_OT_add_driven_path,
    SDK_OT_paste_driven_item_path,
    SDK_OT_set_driven_item_index,
    SDK_OT_split_driven_array,
    SDK_OT_capture_driven_item_rest,
    SDK_OT_paste_custom_channel,
    SDK_OT_clear_driven,
    SDK_OT_pick_driver_from_context,
    SDK_OT_pick_driven_from_context,
    SDK_OT_capture_driver_rest,
    SDK_OT_driver_to_rest,
    SDK_OT_jump_key,
    SDK_OT_channel_preset,
    SDK_OT_load_driven,
    SDK_OT_load_shape_keys,
    SDK_OT_remove_driven,
    SDK_OT_capture_base,
    SDK_OT_reset_base,
    SDK_OT_scrub_range_auto,
    SDK_OT_action_refresh,
    SDK_OT_mirror_keys,
    SDK_OT_key,
    SDK_OT_delete_key,
    SDK_OT_delete_curves,
    SDK_UL_configs,
    SDK_UL_driven,
    SDK_PT_main,
    SDK_PT_options,
    SDK_PT_configs,
)

_HANDLER_LISTS = (
    bpy.app.handlers.undo_post,
    bpy.app.handlers.redo_post,
    bpy.app.handlers.load_post,
)


def register():
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except ValueError as e:
            # Already registered (reload or dual legacy + extension install) is expected;
            # anything else is a real problem, so make it visible.
            if "already registered" not in str(e):
                print("[SDK] could not register %s: %s" % (c.__name__, e))
    if not hasattr(bpy.types.Scene, "sdk_settings"):
        bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)
    for handlers in _HANDLER_LISTS:
        if _on_history_change not in handlers:
            handlers.append(_on_history_change)
    mt = _context_menu_type()
    if mt is not None:
        mt.append(_draw_button_context_menu)


def unregister():
    mt = _context_menu_type()
    if mt is not None:
        try:
            mt.remove(_draw_button_context_menu)
        except ValueError:
            pass
    for handlers in _HANDLER_LISTS:
        if _on_history_change in handlers:
            handlers.remove(_on_history_change)
    cleanup_dummy_target()
    if hasattr(bpy.types.Scene, "sdk_settings"):
        del bpy.types.Scene.sdk_settings
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
