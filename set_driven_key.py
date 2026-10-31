bl_info = {
    "name": "Set Driven Key",
    "author": "CloudKing",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "3D Viewport > Sidebar (N) > Driven Key",
    "description": "Set Driven Key with an additive mode: driven offsets stack on top of your own animation",
    "category": "Animation",
}

import re
import traceback

import bpy
from bpy.props import (
    BoolProperty,
    BoolVectorProperty,
    CollectionProperty,
    EnumProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Euler, Quaternion

# Name of the driver variable the add-on creates. It is also how "our" driver
# F-curves are recognised, so hand-made drivers are never touched.
SDK_VAR = "sdk_driver"

# Additive mode on pose bones uses one Transformation constraint per kind.
SDK_CON = {"loc": "SDK Location", "rot": "SDK Rotation", "scale": "SDK Scale"}
CON_SUFFIX = {"loc": "", "rot": "_rot", "scale": "_scale"}
DELTA_ATTR = {"loc": "delta_location", "rot": "delta_rotation_euler", "scale": "delta_scale"}
DIRECT_ATTR = {"loc": "location", "rot": "rotation_euler", "scale": "scale"}

KEY_EPS = 0.01  # Blender treats keys closer than this (on the X axis) as the same key


# --------------------------------------------------------------------------
# Path / value helpers
# --------------------------------------------------------------------------

_CUSTOM_RE = re.compile(r"^(.+)\[(\d+)\]$")


def parse_custom(text):
    """'location[1]' -> ('location', 1);  '["prop"]' -> ('["prop"]', -1)"""
    text = text.strip()
    m = _CUSTOM_RE.match(text)
    if m:
        return m.group(1), int(m.group(2))
    return text, -1


def full_path(bone, rel):
    """Data path relative to the Object, optionally going through a pose bone."""
    if not bone:
        return rel
    base = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone)
    return base + (rel if rel.startswith("[") else "." + rel)


def var_path(path, index):
    return "%s[%d]" % (path, index) if index >= 0 else path


def get_value(obj, path, index):
    value = obj.path_resolve(path)
    if index >= 0:
        value = value[index]
    return float(value)


# Matches a data path that ends in a custom (ID) property: <parent>["name"] or <parent>["name"][2]
_IDPROP_RE = re.compile(
    r'^(?P<parent>.*?)\[(?P<q>["\'])(?P<name>(?:\\.|(?!(?P=q)).)*)(?P=q)\]'
    r'(?:\[(?P<idx>\d+)\])?$'
)


def set_by_path(obj, path, index, value):
    """Assign `value` to the property at `path` (optionally an array element).

    Handles both RNA attributes ('location', 'pose.bones["b"].constraints["c"].to_min_x')
    and custom/ID properties ('["prop"]', 'pose.bones["b"]["prop"]'), which can't be set
    with setattr() and need item assignment instead.
    """
    m = _IDPROP_RE.match(path)
    if m:
        parent = m.group("parent")
        owner = obj.path_resolve(parent) if parent else obj
        name = bpy.utils.unescape_identifier(m.group("name"))
        idx = int(m.group("idx")) if m.group("idx") is not None else index
        if idx >= 0:
            owner[name][idx] = value
        else:
            owner[name] = value
        return

    if "." in path:
        parent, prop = path.rsplit(".", 1)
        owner = obj.path_resolve(parent)
    else:
        owner, prop = obj, path
    if index >= 0:
        getattr(owner, prop)[index] = value
    else:
        setattr(owner, prop, value)


def identity_for(path):
    return 1.0 if path.endswith("_scale") else 0.0


def owner_of(item):
    """The Object or PoseBone whose base channels this item represents."""
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
        'Any data path, e.g. ["myprop"], location[2] or modifiers["Bevel"].width',
    ))
    return items


DRIVER_ITEMS = _build_driver_items()


def driver_spec(s):
    if s.driver_channel == "CUSTOM":
        return parse_custom(s.driver_custom)
    attr, idx = s.driver_channel.split(":")
    return attr, int(idx)


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
    return [parse_custom(t) for t in s.ch_custom.split(";") if t.strip()]


# --------------------------------------------------------------------------
# Driver / curve helpers
# --------------------------------------------------------------------------

def find_driver_fcurve(obj, path, index):
    ad = obj.animation_data
    if ad is None:
        return None
    idx = max(index, 0)
    for fc in ad.drivers:
        if fc.data_path == path and fc.array_index == idx:
            return fc
    return None


def is_sdk_fcurve(fc):
    return any(v.name == SDK_VAR for v in fc.driver.variables)


def ensure_driver(obj, path, index, drv_obj, drv_data_path, mute):
    """Return (fcurve, message). fcurve is None if the channel can't be used."""
    fc = find_driver_fcurve(obj, path, index)
    if fc is None:
        try:
            fc = obj.driver_add(path, index) if index >= 0 else obj.driver_add(path)
        except Exception as e:  # noqa: BLE001
            return None, "Cannot drive '%s': %s" % (path, e)
        if isinstance(fc, (list, tuple)):
            obj.driver_remove(path)
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (path, path)
        fc.mute = mute
    elif not is_sdk_fcurve(fc):
        return None, "'%s' already has a driver that wasn't made by this add-on" % path

    drv = fc.driver
    var = next((v for v in drv.variables if v.name == SDK_VAR), None)
    same = (
        var is not None
        and var.targets[0].id == drv_obj
        and var.targets[0].data_path == drv_data_path
    )
    if not same:
        # New / different driver: old keys would be meaningless, start fresh.
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[0], fast=True)
        for v in list(drv.variables):
            drv.variables.remove(v)
        var = drv.variables.new()
        var.name = SDK_VAR
        var.type = "SINGLE_PROP"
        t = var.targets[0]
        t.id_type = "OBJECT"
        t.id = drv_obj
        t.data_path = drv_data_path
        drv.type = "SUM"  # single variable -> passes the value straight through

    # Python-made drivers come with a Generator modifier that would bypass the keys.
    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    return fc, ""


def key_curve(fc, x, y, interpolation, extrapolation):
    """Set the key at driver value `x` to `y` (replacing a key already there).

    keyframe_points.insert() can return None in recent Blender versions, so keys are
    edited / added directly instead. Point properties are set BEFORE fc.update(),
    because update() re-sorts the points and would invalidate the reference.
    """
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


def safe_call(fn, label, *args):
    """Run one key operation. A failure becomes a message instead of aborting everything."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return "%s failed: %s" % (label, e)


# --------------------------------------------------------------------------
# Additive mode helpers
# --------------------------------------------------------------------------

def additive_targets(item, kind, axis):
    """(path, index) pairs the SDK drives for one transform channel in additive mode.

    Objects use delta transforms (one property). Pose bones use a Transformation
    constraint whose min/max output are both driven to the same value, so the
    output is constant and independent of the constraint's source.
    """
    if not item.bone:
        return [(DELTA_ATTR[kind], axis)]
    a = "xyz"[axis]
    rel = 'constraints["%s"].' % SDK_CON[kind]
    return [
        (full_path(item.bone, rel + "to_%s_%s%s" % (m, a, CON_SUFFIX[kind])), -1)
        for m in ("min", "max")
    ]


def ensure_constraint(item, kind, drv_obj, drv_bone, mute):
    pb = owner_of(item)
    name = SDK_CON[kind]
    con = pb.constraints.get(name)
    if con is None:
        con = pb.constraints.new("TRANSFORM")
        con.name = name
        con.owner_space = "LOCAL"
        con.target_space = "LOCAL"
        con.map_from = "LOCATION"
        con.use_motion_extrapolate = True
        for a in "xyz":
            setattr(con, "from_min_" + a, 0.0)
            setattr(con, "from_max_" + a, 1.0)
        con.map_to_x_from = "X"
        con.map_to_y_from = "Y"
        con.map_to_z_from = "Z"
        ident = 1.0 if kind == "scale" else 0.0
        for a in "xyz":
            setattr(con, "to_min_%s%s" % (a, CON_SUFFIX[kind]), ident)
            setattr(con, "to_max_%s%s" % (a, CON_SUFFIX[kind]), ident)
        if kind == "loc":
            con.map_to = "LOCATION"
            con.mix_mode_loc = "ADD"
        elif kind == "rot":
            con.map_to = "ROTATION"
            con.mix_mode_rot = "ADD"
        else:
            con.map_to = "SCALE"
            con.mix_mode_scale = "MULTIPLY"
        con.mute = mute
    # The target only exists to keep the constraint valid; the output doesn't use it.
    con.target = drv_obj
    con.subtarget = drv_bone or ""
    return con


def capture(item):
    """Snapshot the base channels (the 'rest' that additive offsets are measured from)."""
    t = owner_of(item)
    if t is None:
        return
    item.rest_loc = t.location
    item.rest_euler = t.rotation_euler
    item.rest_quat = t.rotation_quaternion
    item.rest_scale = t.scale


def current_euler(owner):
    if owner.rotation_mode == "QUATERNION":
        return owner.rotation_quaternion.to_euler()
    return owner.rotation_euler.copy()


def rest_euler(item, owner):
    if owner.rotation_mode == "QUATERNION":
        return Quaternion(item.rest_quat).to_euler()
    return Euler(item.rest_euler)


def reset_base(item, specs):
    owner = owner_of(item)
    if owner is None:
        return
    for kind, axis in specs:
        if kind == "loc":
            owner.location[axis] = item.rest_loc[axis]
        elif kind == "scale":
            owner.scale[axis] = item.rest_scale[axis]
        else:
            if owner.rotation_mode == "QUATERNION":
                owner.rotation_quaternion = item.rest_quat
            else:
                owner.rotation_euler[axis] = item.rest_euler[axis]


def key_additive(s, item, kind, axis, drv_obj, d_path, d_idx, d_var_path, x):
    """Key one transform channel as an offset from the captured base. Returns '' or a message."""
    obj = item.obj
    owner = owner_of(item)
    if item.bone and obj == drv_obj and item.bone == s.driver_bone:
        return "Bone '%s' can't drive itself" % item.bone

    # Offset the user has posed relative to the captured base.
    if kind == "loc":
        base_off = owner.location[axis] - item.rest_loc[axis]
    elif kind == "rot":
        mode = owner.rotation_mode
        if mode == "AXIS_ANGLE":
            return "Axis-angle rotation isn't supported"
        if not item.bone and mode == "QUATERNION":
            return "Additive rotation on objects needs Euler rotation mode"
        base_off = current_euler(owner)[axis] - rest_euler(item, owner)[axis]
        if mode == "QUATERNION":  # wrap so conversion jumps don't become huge offsets
            base_off = (base_off + 3.14159265358979) % 6.28318530717959 - 3.14159265358979
    else:
        rest = item.rest_scale[axis]
        if abs(rest) < 1e-8:
            return "Base scale is zero on %s, can't compute a ratio" % obj.name
        base_off = owner.scale[axis] / rest

    targets = additive_targets(item, kind, axis)
    for path, idx in targets:
        if obj == drv_obj and path == d_path and max(idx, 0) == max(d_idx, 0):
            return "'%s' can't drive itself" % path

    # What-you-see-is-what-you-key: include the offset that is currently being shown.
    ident = 1.0 if kind == "scale" else 0.0
    existing = ident
    if s.live_preview:
        p0, i0 = targets[0]
        if find_driver_fcurve(obj, p0, i0) is not None:
            try:
                existing = get_value(obj, p0, i0)
            except Exception:  # noqa: BLE001
                existing = ident
    value = existing * base_off if kind == "scale" else existing + base_off

    if item.bone:
        try:
            ensure_constraint(item, kind, drv_obj, s.driver_bone, not s.live_preview)
        except Exception as e:  # noqa: BLE001
            return "Can't set up constraint on '%s': %s" % (item.bone, e)

    for path, idx in targets:
        mute = False if item.bone else not s.live_preview
        fc, msg = ensure_driver(obj, path, idx, drv_obj, d_var_path, mute)
        if fc is None:
            return msg
        key_curve(fc, x, value, s.interpolation, s.extrapolation)
    return ""


def key_direct(s, item, attr, idx, drv_obj, d_path, d_idx, d_var_path, x):
    """Maya-style: the driver takes over the channel. Returns '' or a message."""
    obj = item.obj
    path = full_path(item.bone, attr)
    if obj == drv_obj and path == d_path and max(idx, 0) == max(d_idx, 0):
        return "'%s' can't drive itself" % path
    try:
        y = get_value(obj, path, idx)  # read BEFORE a driver can overwrite it
    except Exception:  # noqa: BLE001
        return "Can't read '%s'" % var_path(path, idx)
    fc, msg = ensure_driver(obj, path, idx, drv_obj, d_var_path, not s.live_preview)
    if fc is None:
        return msg
    key_curve(fc, x, y, s.interpolation, s.extrapolation)
    return ""


def all_targets(s, item):
    """Every (path, index) the enabled channels could be driven through, in either mode."""
    out = []
    for kind, axis in transform_specs(s):
        out.append((full_path(item.bone, DIRECT_ATTR[kind]), axis))
        out.extend(additive_targets(item, kind, axis))
    for i in quat_specs(s):
        out.append((full_path(item.bone, "rotation_quaternion"), i))
    for rel, idx in custom_specs(s):
        out.append((full_path(item.bone, rel), idx))
    return out


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _preview_update(self, context):
    """Live Preview off = SDK effect disabled, so you can see/pose the raw base values."""
    on = self.live_preview
    seen = set()
    for item in self.driven_items:
        obj = item.obj
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        ad = obj.animation_data
        if ad:
            for fc in ad.drivers:
                if not is_sdk_fcurve(fc) or ".constraints[" in fc.data_path:
                    continue
                fc.mute = not on
                if not on and fc.data_path.startswith("delta_"):
                    try:
                        set_by_path(obj, fc.data_path, fc.array_index, identity_for(fc.data_path))
                    except Exception:  # noqa: BLE001
                        pass
        if obj.pose:
            for pb in obj.pose.bones:
                for con in pb.constraints:
                    if con.name in SDK_CON.values():
                        con.mute = not on


class SDK_DrivenItem(PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object)
    bone: StringProperty()
    # Captured base (additive mode measures offsets from this)
    rest_loc: FloatVectorProperty(size=3)
    rest_euler: FloatVectorProperty(size=3)
    rest_quat: FloatVectorProperty(size=4, default=(1.0, 0.0, 0.0, 0.0))
    rest_scale: FloatVectorProperty(size=3, default=(1.0, 1.0, 1.0))


class SDK_Settings(PropertyGroup):
    # Driver
    driver_obj: PointerProperty(type=bpy.types.Object)
    driver_bone: StringProperty()
    driver_channel: EnumProperty(
        name="Channel", items=DRIVER_ITEMS, default="location:0",
        description="Driver channel",
    )
    driver_custom: StringProperty(
        name="Path", description='Data path, e.g. ["myprop"] or location[2]',
    )

    # Driven
    driven_items: CollectionProperty(type=SDK_DrivenItem)
    driven_index: IntProperty()
    ch_loc: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_rot: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_quat: BoolVectorProperty(size=4, default=(False,) * 4)
    ch_scale: BoolVectorProperty(size=3, default=(False,) * 3)
    ch_custom: StringProperty(
        name="Custom",
        description='Extra driven data paths separated by ";", e.g. ["myprop"];location[2]. '
                    "Custom paths are always Direct",
    )

    # Behaviour
    mode: EnumProperty(
        name="Mode",
        items=[
            ("ADDITIVE", "Additive",
             "Non-destructive: the driven offset is stacked on top of the channel, which stays "
             "free to keyframe, pose and animate (delta transforms on objects, a Transformation "
             "constraint on bones)"),
            ("DIRECT", "Direct",
             "Maya-style: the driver takes over the channel and sets its value"),
        ],
        default="ADDITIVE",
    )
    auto_reset: BoolProperty(
        name="Reset to Base after Key",
        description="After keying, put the posed channels back to the captured base. "
                    "The pose on screen stays the same because the offset now provides it",
        default=True,
    )
    interpolation: EnumProperty(
        name="Interpolation",
        items=[
            ("BEZIER", "Smooth", "Smooth curve between keys"),
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
        description="On: the SDK takes effect. Off: the SDK is disabled so you can see and "
                    "pose the raw channels",
        default=True,
        update=_preview_update,
    )


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class SDK_OT_load_driver(Operator):
    bl_idname = "sdk.load_driver"
    bl_label = "Load Driver"
    bl_description = "Use the active object (or active bone in Pose Mode) as the driver"

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
        return {"FINISHED"}


class SDK_OT_load_driven(Operator):
    bl_idname = "sdk.load_driven"
    bl_label = "Load Driven"
    bl_description = ("Use the selected objects (or selected bones in Pose Mode) as driven. "
                      "Their current channels are captured as the base")

    append: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        s = context.scene.sdk_settings
        if context.mode == "POSE" and context.selected_pose_bones:
            entries = [(context.active_object, pb.name) for pb in context.selected_pose_bones]
        else:
            entries = [(o, "") for o in context.selected_objects]
        if not entries:
            self.report({"ERROR"}, "Nothing selected")
            return {"CANCELLED"}
        if not self.append:
            s.driven_items.clear()
        existing = {(i.obj.as_pointer() if i.obj else 0, i.bone) for i in s.driven_items}
        for obj, bone in entries:
            key = (obj.as_pointer(), bone)
            if key in existing:
                continue
            existing.add(key)
            item = s.driven_items.add()
            item.obj = obj
            item.bone = bone
            capture(item)
        return {"FINISHED"}


class SDK_OT_remove_driven(Operator):
    bl_idname = "sdk.remove_driven"
    bl_label = "Remove Driven"
    bl_description = "Remove the highlighted entry from the driven list"

    def execute(self, context):
        s = context.scene.sdk_settings
        if 0 <= s.driven_index < len(s.driven_items):
            s.driven_items.remove(s.driven_index)
            s.driven_index = max(0, s.driven_index - 1)
        return {"FINISHED"}


class SDK_OT_capture_base(Operator):
    bl_idname = "sdk.capture_base"
    bl_label = "Capture Base"
    bl_description = ("Re-capture the current channel values of the driven items as the base "
                      "that additive offsets are measured from")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for item in context.scene.sdk_settings.driven_items:
            capture(item)
        return {"FINISHED"}


class SDK_OT_reset_base(Operator):
    bl_idname = "sdk.reset_base"
    bl_label = "Reset to Base"
    bl_description = "Put the enabled driven channels back to the captured base values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        specs = transform_specs(s)
        for item in s.driven_items:
            reset_base(item, specs)
        context.view_layer.update()
        return {"FINISHED"}


class SDK_OT_key(Operator):
    bl_idname = "sdk.key"
    bl_label = "Key"
    bl_description = ("Store the driven channels against the current driver value "
                      "(creates the driver and curve if needed)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        drv_obj = s.driver_obj
        if drv_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}

        d_rel, d_idx = driver_spec(s)
        d_path = full_path(s.driver_bone, d_rel)
        d_var_path = var_path(d_path, d_idx)
        try:
            x = get_value(drv_obj, d_path, d_idx)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver property '%s'" % d_var_path)
            return {"CANCELLED"}

        if not len(s.driven_items):
            self.report({"ERROR"}, "Load driven objects first")
            return {"CANCELLED"}
        tspecs, qspecs, cspecs = transform_specs(s), quat_specs(s), custom_specs(s)
        if not (tspecs or qspecs or cspecs):
            self.report({"ERROR"}, "Enable at least one driven channel")
            return {"CANCELLED"}

        additive = s.mode == "ADDITIVE"
        problems = []
        if additive and qspecs:
            problems.append("Quaternion channels are Direct-mode only")

        keyed = 0
        for item in s.driven_items:
            obj = item.obj
            if obj is None:
                continue
            if owner_of(item) is None:
                problems.append("'%s' not found" % (item.bone or obj.name))
                continue

            done = []
            for kind, axis in tspecs:
                label = "%s %s%s" % (item.bone or obj.name, kind, "XYZ"[axis])
                if additive:
                    msg = safe_call(key_additive, label, s, item, kind, axis,
                                    drv_obj, d_path, d_idx, d_var_path, x)
                else:
                    msg = safe_call(key_direct, label, s, item, DIRECT_ATTR[kind], axis,
                                    drv_obj, d_path, d_idx, d_var_path, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1
                    done.append((kind, axis))
            if not additive:
                for i in qspecs:
                    msg = safe_call(key_direct, "%s quat %s" % (item.bone or obj.name, "WXYZ"[i]),
                                    s, item, "rotation_quaternion", i,
                                    drv_obj, d_path, d_idx, d_var_path, x)
                    if msg:
                        problems.append(msg)
                    else:
                        keyed += 1
            for rel, idx in cspecs:
                msg = safe_call(key_direct, "%s %s" % (item.bone or obj.name, rel),
                                s, item, rel, idx, drv_obj, d_path, d_idx, d_var_path, x)
                if msg:
                    problems.append(msg)
                else:
                    keyed += 1

            if additive and s.auto_reset and done:
                reset_base(item, done)

        context.view_layer.update()
        if problems:
            self.report({"WARNING"}, "; ".join(sorted(set(problems))[:3]))
        if not keyed:
            return {"CANCELLED"}
        self.report({"INFO"}, "Keyed %d channel(s) at driver value %.4f" % (keyed, x))
        return {"FINISHED"}


class SDK_OT_delete_key(Operator):
    bl_idname = "sdk.delete_key"
    bl_label = "Delete Key at Driver Value"
    bl_description = "Remove the key at the current driver value from the enabled driven channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        if s.driver_obj is None:
            self.report({"ERROR"}, "Load a driver first")
            return {"CANCELLED"}
        d_rel, d_idx = driver_spec(s)
        try:
            x = get_value(s.driver_obj, full_path(s.driver_bone, d_rel), d_idx)
        except Exception:  # noqa: BLE001
            self.report({"ERROR"}, "Can't read driver property")
            return {"CANCELLED"}

        removed = 0
        for item in s.driven_items:
            obj = item.obj
            if obj is None:
                continue
            for path, idx in all_targets(s, item):
                fc = find_driver_fcurve(obj, path, idx)
                if fc is None or not is_sdk_fcurve(fc):
                    continue
                for kp in fc.keyframe_points:
                    if abs(kp.co.x - x) < KEY_EPS:
                        fc.keyframe_points.remove(kp)
                        fc.update()
                        removed += 1
                        break
        self.report({"INFO"}, "Removed %d key(s)" % removed)
        return {"FINISHED"}


class SDK_OT_delete_curves(Operator):
    bl_idname = "sdk.delete_curves"
    bl_label = "Delete Driven Curves"
    bl_description = "Remove the driven-key curves from the enabled driven channels"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sdk_settings
        removed = 0
        for item in s.driven_items:
            obj = item.obj
            if obj is None:
                continue
            for path, idx in all_targets(s, item):
                fc = find_driver_fcurve(obj, path, idx)
                if fc is None or not is_sdk_fcurve(fc):
                    continue
                try:
                    obj.driver_remove(path, idx)
                except Exception:  # noqa: BLE001
                    continue
                removed += 1
                if path.startswith("delta_") or ".constraints[" in path:
                    try:  # additive outputs go back to "no offset"
                        set_by_path(obj, path, idx, identity_for(path))
                    except Exception:  # noqa: BLE001
                        pass

            # Drop SDK constraints that no longer have any driven output.
            if item.bone and obj.pose and item.bone in obj.pose.bones:
                pb = obj.pose.bones[item.bone]
                drivers = list(obj.animation_data.drivers) if obj.animation_data else []
                for name in SDK_CON.values():
                    con = pb.constraints.get(name)
                    if con is None:
                        continue
                    prefix = full_path(item.bone, 'constraints["%s"]' % name)
                    if not any(fc.data_path.startswith(prefix) for fc in drivers):
                        pb.constraints.remove(con)
        context.view_layer.update()
        self.report({"INFO"}, "Removed %d driven curve(s)" % removed)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

class SDK_UL_driven(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
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

        # ---- Driver -------------------------------------------------------
        box = layout.box()
        box.label(text="Driver", icon="DRIVER")
        box.operator("sdk.load_driver", icon="EYEDROPPER")
        if s.driver_obj:
            name = s.driver_obj.name + (" : " + s.driver_bone if s.driver_bone else "")
            box.label(text=name, icon="BONE_DATA" if s.driver_bone else "OBJECT_DATA")
            box.prop(s, "driver_channel", text="")
            if s.driver_channel == "CUSTOM":
                box.prop(s, "driver_custom")
                rel, idx = driver_spec(s)
                try:
                    val = get_value(s.driver_obj, full_path(s.driver_bone, rel), idx)
                    box.label(text="Value: %.4f" % val)
                except Exception:  # noqa: BLE001
                    box.label(text="Invalid path", icon="ERROR")
            else:
                attr, idx = driver_spec(s)
                try:
                    owner = s.driver_obj.pose.bones[s.driver_bone] if s.driver_bone else s.driver_obj
                    box.prop(owner, attr, index=idx, text="Value")
                except Exception:  # noqa: BLE001
                    box.label(text="Bone not found", icon="ERROR")
        else:
            box.label(text="No driver loaded", icon="INFO")

        # ---- Driven -------------------------------------------------------
        box = layout.box()
        box.label(text="Driven", icon="LINKED")
        row = box.row(align=True)
        row.operator("sdk.load_driven", text="Load Driven", icon="EYEDROPPER").append = False
        row.operator("sdk.load_driven", text="Add", icon="ADD").append = True
        row = box.row()
        row.template_list("SDK_UL_driven", "", s, "driven_items", s, "driven_index", rows=3)
        row.operator("sdk.remove_driven", text="", icon="REMOVE")

        col = box.column(align=True)
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
        box.prop(s, "ch_custom", text="Custom")

        # ---- Mode ---------------------------------------------------------
        layout.prop(s, "mode", expand=True)
        if s.mode == "ADDITIVE":
            row = layout.row(align=True)
            row.operator("sdk.capture_base", icon="PINNED")
            row.operator("sdk.reset_base", icon="LOOP_BACK")
            layout.prop(s, "auto_reset")

        # ---- Actions ------------------------------------------------------
        row = layout.row(align=True)
        row.scale_y = 1.6
        row.operator("sdk.key", icon="KEY_HLT")
        row = layout.row(align=True)
        row.operator("sdk.delete_key", text="Delete Key", icon="KEY_DEHLT")
        row.operator("sdk.delete_curves", text="Delete Curves", icon="TRASH")
        layout.prop(
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


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

classes = (
    SDK_DrivenItem,
    SDK_Settings,
    SDK_OT_load_driver,
    SDK_OT_load_driven,
    SDK_OT_remove_driven,
    SDK_OT_capture_base,
    SDK_OT_reset_base,
    SDK_OT_key,
    SDK_OT_delete_key,
    SDK_OT_delete_curves,
    SDK_UL_driven,
    SDK_PT_main,
    SDK_PT_options,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)


def unregister():
    del bpy.types.Scene.sdk_settings
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
