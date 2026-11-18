"""Set Driven Key — props."""

import bpy
from bpy.props import (
    BoolProperty,
    BoolVectorProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from .const import _READ_ERRORS
from .paths import inspect_rna_target
from .drivers import DRIVER_ITEMS, auto_scrub_range, capture_driver_rest, read_driver_value, write_driver_value
from .constraints import capture_custom_rest, set_sdk_muted_for_settings
from .query import invalidate_key_cache



class SDK_ConfigObjRef(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)


class SDK_ConfigItem(PropertyGroup):
    name: StringProperty(name="Name", default="SDK_Config")
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_ref: StringProperty(default="{}")
    driver_bone: StringProperty()
    driver_channel: StringProperty(default="location:0")
    driver_custom: StringProperty()
    driver_rest: FloatProperty(default=0.0)
    mode: StringProperty(default="ADDITIVE")
    config_layer_tag: StringProperty(default="")
    ch_loc: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_rot: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_quat: BoolVectorProperty(size=4, default=(False,) * 4)
    ch_scale: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_custom: StringProperty(default="")
    driven_data: StringProperty(default="[]")
    driven_refs: CollectionProperty(type=SDK_ConfigObjRef)


def _preview_update(self, context):
    set_sdk_muted_for_settings(self, not self.live_preview)


def _driver_changed(self, context):
    capture_driver_rest(self)
    auto_scrub_range(self)
    invalidate_key_cache()


def _ch_custom_updated(self, context):
    for item in self.driven_items:
        if item.target_kind == "OBJECT":
            capture_custom_rest(item, self, only_missing=True)
    invalidate_key_cache()


def _rna_path_updated(self, context):
    if self.target_kind == "RNA_PATH":
        self.has_rest_rna = False
        info = inspect_rna_target(None, self.rna_path)
        if info.ok and info.value is not None:
            self.rest_rna = info.value
            self.has_rest_rna = True
    invalidate_key_cache()


def _driver_current_get(self):
    try:
        return read_driver_value(self)
    except _READ_ERRORS:
        return 0.0


def _driver_current_set(self, value):
    try:
        write_driver_value(self, value)
        if bpy.context and bpy.context.view_layer:
            bpy.context.view_layer.update()
    except _READ_ERRORS:
        pass


def _scrub_t_get(self):
    lo, hi = self.scrub_min, self.scrub_max
    if hi - lo < 1e-9:
        return 0.0
    try:
        v = read_driver_value(self)
    except _READ_ERRORS:
        return 0.0
    return min(1.0, max(0.0, (v - lo) / (hi - lo)))


def _scrub_t_set(self, t):
    lo, hi = self.scrub_min, self.scrub_max
    _driver_current_set(self, lo + t * (hi - lo))


class SDK_DrivenItem(PropertyGroup):
    target_kind: EnumProperty(
        name="Target Kind",
        items=[
            ("OBJECT", "Object / Bone", "Object or Pose Bone target"),
            ("RNA_PATH", "RNA Path", "Any full RNA data path"),
        ],
        default="OBJECT",
    )
    obj: PointerProperty(type=bpy.types.Object)
    bone: StringProperty()
    rna_path: StringProperty(
        name="RNA Path",
        description="Full bpy.data... RNA data path for this driven property",
        update=_rna_path_updated,
    )
    rest_rna: FloatProperty(name="Rest Value", default=0.0)
    has_rest_rna: BoolProperty(default=False)
    rest_loc: FloatVectorProperty(size=3)
    rest_euler: FloatVectorProperty(size=3)
    rest_quat: FloatVectorProperty(size=4, default=(1.0, 0.0, 0.0, 0.0))
    rest_scale: FloatVectorProperty(size=3, default=(1.0, 1.0, 1.0))
    rest_custom: StringProperty(default="{}")


class SDK_Settings(PropertyGroup):
    # Configurations
    configs: CollectionProperty(type=SDK_ConfigItem)
    config_index: IntProperty(default=0)
    config_layer_tag: StringProperty(
        name="Layer Tag",
        description="Unique tag for this configuration layer. When set, creates a new stacked "
                    "transform constraint / action layer instead of overwriting existing ones",
        default="",
    )

    # Driver
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_bone: StringProperty()
    driver_channel: EnumProperty(
        name="Channel", items=DRIVER_ITEMS, default="location:0",
        description="Driver channel", update=_driver_changed,
    )
    driver_custom: StringProperty(
        name="Path",
        description='Any RNA path, e.g. ["myprop"], location[2], or bpy.data.materials["Mat"].node_tree.nodes["Value"].outputs[0].default_value',
        update=_driver_changed,
    )
    driver_rest: FloatProperty(
        name="Rest",
        description="Driver value at which the driven channels are in their original state. "
                    "Captured on load or channel change; editable",
    )
    driver_scrub: FloatProperty(
        name="Scrub Driver",
        description="Scrub current driver property value directly from the UI",
        get=_driver_current_get,
        set=_driver_current_set,
    )
    driver_scrub_t: FloatProperty(
        name="Scrub",
        description="Scrub the driver between Min and Max",
        subtype="FACTOR", min=0.0, max=1.0,
        get=_scrub_t_get,
        set=_scrub_t_set,
    )
    scrub_min: FloatProperty(name="Min", description="Driver value at the left end of the scrub slider", default=-1.0)
    scrub_max: FloatProperty(name="Max", description="Driver value at the right end of the scrub slider", default=1.0)
    auto_rest_key: BoolProperty(
        name="Auto Rest Key",
        description="When a driven curve gets its first key, also key the rest state at the "
                    "driver's rest value, so driven channels return to base when driver does",
        default=True,
    )

    # Driven
    driven_items: CollectionProperty(type=SDK_DrivenItem)
    driven_index: IntProperty()
    ch_loc: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_rot: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_quat: BoolVectorProperty(size=4, default=(False,) * 4)
    ch_scale: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_custom: StringProperty(
        name="Extra Paths",
        description='Extra driven data paths separated by ";" (supports ["prop"], data.energy, '
                    'data.shape_keys.key_blocks["Key"].value, or full bpy.data... paths). Uses Direct drivers',
        update=_ch_custom_updated,
    )

    # Behaviour
    mode: EnumProperty(
        name="Mode",
        items=[
            ("ADDITIVE", "Additive (Multi-Axis)",
             "Non-destructive: driven transforms stack on top of base animation. "
             "(Custom RNA paths always use Direct drivers)"),
            ("DIRECT", "Direct",
             "Maya-style: driver takes over channels directly"),
            ("ACTION", "Action Constraint",
             "Professional rigging: driver drives an Action Constraint eval range. "
             "(Custom RNA paths always use Direct drivers)"),
        ],
        default="ADDITIVE",
    )
    auto_reset: BoolProperty(
        name="Reset to Base after Key",
        description="After keying, put posed channels back to the captured base (Additive & Action modes)",
        default=True,
    )
    interpolation: EnumProperty(
        name="Interpolation",
        items=[
            ("BEZIER", "Smooth", "Smooth Bezier curve between keys"),
            ("LINEAR", "Linear", "Straight lines between keys"),
            ("CONSTANT", "Stepped", "Hold value until next key"),
        ],
        default="BEZIER",
    )
    extrapolation: EnumProperty(
        name="Extrapolation",
        items=[
            ("CONSTANT", "Constant", "Hold the first/last value outside the keyed range"),
            ("LINEAR", "Linear", "Continue the slope outside the keyed range"),
        ],
        default="CONSTANT",
    )
    live_preview: BoolProperty(
        name="Live Preview",
        description="On: SDK takes effect. Off: SDK is disabled so you can inspect raw poses",
        default=True,
        update=_preview_update,
    )
