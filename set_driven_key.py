# bl_info is kept for legacy "Install from Disk" of this single file.
# When packaged as a Blender Extension, metadata lives in blender_manifest.toml.
bl_info = {
    "name": "Set Driven Key",
    "author": "OceanMan, CloudKing, Apixelitonomo, U.F.Olive",
    "version": (1, 7, 0),
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

import json
import math
import re
import traceback
import zlib
from types import SimpleNamespace

import bpy
from bpy.app.handlers import persistent
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
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper
from mathutils import Euler, Matrix, Quaternion

# Name of the driver variable the add-on creates.
SDK_VAR = "sdk_driver"

# Constraint naming prefixes
SDK_CON_BASE = {"loc": "SDK Location", "rot": "SDK Rotation", "scale": "SDK Scale"}
CON_SUFFIX = {"loc": "", "rot": "_rot", "scale": "_scale"}
DIRECT_ATTR = {"loc": "location", "rot": "rotation_euler", "scale": "scale"}
_ATTR_KIND = {v: k for k, v in DIRECT_ATTR.items()}
_CHANNEL_ATTRS = ("location", "rotation_euler", "rotation_quaternion", "scale")

# Action constraint naming
SDK_ACTION_PREFIX = "SDK_Action_"
SDK_ACTION_CON_PREFIX = "SDK Action ["

# Action Constraint mapping: driver value v  ->  action frame v * ACTION_FRAMES_PER_UNIT
ACTION_FRAMES_PER_UNIT = 100.0

KEY_EPS = 0.01  # Blender treats keys closer than this (on the X axis) as the same key

DUMMY_TARGET_NAME = "SDK_Constraint_Target"
DUMMY_COLLECTION_NAME = "SDK_Internal"

MAX_ID_NAME = 63  # bytes available for constraint / action names

# Errors raised by RNA when a path / property / value is invalid.
_RNA_ERRORS = (AttributeError, KeyError, IndexError, TypeError, ValueError, RuntimeError, ReferenceError)
_READ_ERRORS = _RNA_ERRORS + (LookupError,)

# Non-fatal messages collected while an operator runs.
_WARNINGS = []


def _warn(msg):
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)


def _report_problems(op, problems):
    uniq = sorted(set(problems))
    if not uniq:
        return
    for p in uniq:
        print("[SDK] " + p)
    text = "; ".join(uniq[:3])
    if len(uniq) > 3:
        text += " (+%d more, see the system console)" % (len(uniq) - 3)
    op.report({"WARNING"}, text)


# --------------------------------------------------------------------------
# Path / RNA resolution helpers
# --------------------------------------------------------------------------

_CUSTOM_RE = re.compile(r"^(.+)\[(\d+)\]$")
_BPY_DATA_HEAD_RE = re.compile(
    r'^bpy\.data\.(?P<coll>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*'
    r'(?:"(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\')'
    r'(?:\s*,\s*(?:"(?P<ldq>(?:\\.|[^"\\])*)"|\'(?P<lsq>(?:\\.|[^\'\\])*)\'))?'
    r'\s*\]\s*(?P<rest>.*)$'
)
_BONE_SUBPATH_RE = re.compile(
    r'^pose\.bones\[\s*(?:"(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\')\s*\](?:\.(?P<dot>.*)|(?P<brk>\[.*))?$'
)

# One token of a data path: .attr | ["key"] | ['key'] | [int]
_PATH_TOKEN_RE = re.compile(
    r'\.?(?P<attr>[A-Za-z_][A-Za-z0-9_]*)'
    r'|\[(?:"(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\'|(?P<idx>-?\d+))\]'
)


def _is_bpy_data_path(text):
    return (text or "").lstrip().startswith("bpy.data.")


def parse_custom(text):
    """'location[1]' -> ('location', 1);  '["prop"]' -> ('["prop"]', -1)"""
    text = text.strip()
    m = _CUSTOM_RE.match(text)
    if m:
        return m.group(1), int(m.group(2))
    return text, -1


def full_path(bone, rel):
    """Data path relative to the Object, optionally going through a pose bone."""
    if not bone or rel.startswith("pose.bones["):
        return rel
    base = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone)
    return base + (rel if rel.startswith("[") else "." + rel)


def con_data_path(con_name):
    """constraints["<name>"] with the name escaped."""
    return 'constraints["%s"]' % bpy.utils.escape_identifier(con_name)


def var_path(path, index):
    return "%s[%d]" % (path, index) if index >= 0 else path


def _join_rel(base, child):
    if not base:
        return child
    if not child:
        return base
    return base + (child if child.startswith("[") else "." + child)


def _join_full(id_expr, rel):
    if not rel:
        return id_expr
    return id_expr + (rel if rel.startswith("[") else "." + rel)


def _split_path(path):
    """Split a data path into [(kind, value, start)]; kind is 'attr' | 'key' | 'idx'."""
    tokens = []
    pos = 0
    while pos < len(path):
        m = _PATH_TOKEN_RE.match(path, pos)
        if m is None:
            raise ValueError("Unsupported data path '%s'" % path)
        if m.group("attr") is not None:
            tokens.append(("attr", m.group("attr"), pos))
        elif m.group("idx") is not None:
            tokens.append(("idx", int(m.group("idx")), pos))
        else:
            raw = m.group("dq") if m.group("dq") is not None else m.group("sq")
            tokens.append(("key", raw, pos))
        pos = m.end()
    return tokens


def _is_array_like(val):
    if isinstance(val, (str, bytes, dict, bpy.types.bpy_struct, bpy.types.bpy_prop_collection)):
        return False
    if hasattr(val, "keys"):  # IDPropertyGroup
        return False
    return hasattr(val, "__len__")


def resolve_bpy_data_prefix(text):
    """Parse 'bpy.data.<coll>["Name"].<rest>' -> (id_block, subpath, coll_name, id_name, lib)."""
    m = _BPY_DATA_HEAD_RE.match((text or "").strip())
    if m is None:
        raise ValueError("Invalid bpy.data path '%s'" % (text or ""))
    coll_name = m.group("coll")
    raw_name = m.group("dq") if m.group("dq") is not None else m.group("sq")
    id_name = bpy.utils.unescape_identifier(raw_name)
    raw_lib = m.group("ldq") if m.group("ldq") is not None else m.group("lsq")
    lib = bpy.utils.unescape_identifier(raw_lib) if raw_lib is not None else ""
    rest = m.group("rest") or ""
    if rest.startswith("."):
        rest = rest[1:]

    coll = getattr(bpy.data, coll_name, None)
    if coll is None:
        raise LookupError("Unknown bpy.data collection '%s'" % coll_name)
    if lib:
        root_id = None
        try:
            root_id = coll.get((id_name, lib))
        except TypeError:
            root_id = None
        if root_id is None:
            root_id = next(
                (it for it in coll if it.name == id_name and it.library and it.library.filepath == lib),
                None,
            )
    else:
        root_id = coll.get(id_name)
    if root_id is None:
        raise LookupError("'%s' not found in bpy.data.%s" % (id_name, coll_name))
    return root_id, rest, coll_name, id_name, lib


def _extract_bone_name(root_id, path):
    if isinstance(root_id, bpy.types.Object):
        m = _BONE_SUBPATH_RE.match(path)
        if m:
            raw = m.group("dq") if m.group("dq") is not None else m.group("sq")
            return bpy.utils.unescape_identifier(raw)
    return ""


def _make_rna_error(error, array_len=0, anim_id=None, anim_path="", var_id=None, var_data_path="", bone="", rel_attr=""):
    return SimpleNamespace(
        ok=False,
        error=error,
        array_len=array_len,
        anim_id=anim_id,
        anim_path=anim_path,
        index=-1,
        var_id=var_id,
        var_data_path=var_data_path,
        bone=bone,
        rel_attr=rel_attr,
        value=None,
    )


def inspect_rna_target(base_id, path, bone=""):
    """Resolve any RNA path (absolute bpy.data... or relative to base_id) across ID boundaries.

    Returns a SimpleNamespace with:
      ok, error, array_len, anim_id, anim_path, index, var_id, var_data_path, bone, rel_attr, value
    where:
      - anim_id / anim_path / index: ID datablock owning animation_data (e.g. ShaderNodeTree, Key, Light, Object)
      - var_id / var_data_path: non-embedded ID datablock & path suitable for DriverTarget & read/write
    """
    raw = (path or "").strip()
    if not raw:
        return _make_rna_error("Enter an RNA data path")

    try:
        if _is_bpy_data_path(raw):
            root_id, rel_from_root, coll_name, id_name, _lib = resolve_bpy_data_prefix(raw)
            if not rel_from_root:
                return _make_rna_error("Specify a property after bpy.data.%s[\"%s\"]" % (coll_name, id_name))
        else:
            if base_id is None:
                return _make_rna_error("No target loaded (use a full bpy.data... path or load an object)")
            root_id = base_id
            rel_from_root = full_path(bone, raw)

        tokens = _split_path(rel_from_root)
        if not tokens:
            return _make_rna_error("Empty data path")

        array_index = -1
        prop_tokens = tokens
        prop_path_from_root = rel_from_root
        if len(tokens) >= 2 and tokens[-1][0] == "idx":
            if tokens[-1][1] < 0:
                return _make_rna_error("Negative array index is not supported")
            parent_path = rel_from_root[:tokens[-1][2]]
            parent_val = root_id.path_resolve(parent_path)
            if _is_array_like(parent_val):
                if tokens[-1][1] >= len(parent_val):
                    return _make_rna_error(
                        "Index [%d] out of range (length %d)" % (tokens[-1][1], len(parent_val))
                    )
                array_index = tokens[-1][1]
                prop_tokens = tokens[:-1]
                prop_path_from_root = parent_path

        anim_id = root_id
        anim_start = 0
        var_id = root_id
        var_start = 0
        for i in range(len(prop_tokens) - 1):
            next_start = prop_tokens[i + 1][2]
            prefix_path = prop_path_from_root[:next_start]
            sub_val = root_id.path_resolve(prefix_path)
            if isinstance(sub_val, bpy.types.ID):
                anim_id = sub_val
                anim_start = next_start
                if not getattr(sub_val, "is_embedded_data", False):
                    var_id = sub_val
                    var_start = next_start

        anim_path = prop_path_from_root[anim_start:].lstrip(".")
        var_base_path = prop_path_from_root[var_start:].lstrip(".")
        var_data_path = var_path(var_base_path, array_index)
        eff_bone = _extract_bone_name(var_id, var_data_path)

        val = anim_id.path_resolve(anim_path)
        if array_index >= 0:
            val = val[array_index]

        if _is_array_like(val):
            return _make_rna_error(
                "Array property (length %d) — pick an index" % len(val),
                array_len=len(val),
                anim_id=anim_id,
                anim_path=anim_path,
                var_id=var_id,
                var_data_path=var_data_path,
                bone=eff_bone,
                rel_attr=anim_path,
            )

        if not isinstance(val, (int, float, bool)):
            return _make_rna_error(
                "Property is %s (expected float, int, or bool)" % type(val).__name__,
                anim_id=anim_id,
                anim_path=anim_path,
                var_id=var_id,
                var_data_path=var_data_path,
                bone=eff_bone,
                rel_attr=anim_path,
            )

        return SimpleNamespace(
            ok=True,
            error="",
            array_len=0,
            anim_id=anim_id,
            anim_path=anim_path,
            index=array_index,
            var_id=var_id,
            var_data_path=var_data_path,
            bone=eff_bone,
            rel_attr=anim_path,
            value=float(val),
        )
    except _READ_ERRORS as e:
        return _make_rna_error(str(e))


def short_rna_label(path, max_len=36):
    """Readable short display label for an RNA data path in UI lists."""
    text = (path or "").strip()
    if not text:
        return "<empty RNA path>"
    try:
        if _is_bpy_data_path(text):
            m = _BPY_DATA_HEAD_RE.match(text)
            if m:
                raw_name = m.group("dq") if m.group("dq") is not None else m.group("sq")
                id_name = bpy.utils.unescape_identifier(raw_name)
                rest = (m.group("rest") or "").lstrip(".")
                if rest.startswith("node_tree."):
                    rest = rest[len("node_tree."):]
                elif rest.startswith("data.shape_keys."):
                    rest = rest[len("data.shape_keys."):]
                if len(rest) > max_len:
                    rest = "..." + rest[-(max_len - 3):]
                return ("%s : %s" % (id_name, rest)) if rest else id_name
    except Exception:
        pass
    if len(text) > max_len + 12:
        return "..." + text[-(max_len + 9):]
    return text


def _driver_display_name(s, info):
    if s.driver_channel == "CUSTOM":
        if info.var_id is not None:
            base = info.var_id.name
            if info.bone:
                return "%s : %s" % (base, info.bone)
            if info.anim_id is not None and info.anim_id != info.var_id:
                return "%s (%s)" % (base, info.anim_id.bl_rna.name)
            return base
        return "Custom Path"
    if s.driver_obj:
        return s.driver_obj.name + (" : " + s.driver_bone if s.driver_bone else "")
    return "No driver loaded"


def resolve_driver_info(s):
    """Full resolution info for the current Driver settings."""
    layer = getattr(s, "config_layer_tag", "").strip()
    layer_tag = ("_" + re.sub(r"[^a-zA-Z0-9_]", "_", layer)) if layer else ""

    if s.driver_channel == "CUSTOM":
        raw = s.driver_custom.strip()
        info = inspect_rna_target(s.driver_obj, raw, bone=s.driver_bone)
        if info.var_id is not None:
            clean_sub = re.sub(r"[^a-zA-Z0-9_]", "_", info.var_data_path or raw).strip("_")
            head = info.bone or info.var_id.name
            scope_key = "%s_%s%s" % (head, clean_sub, layer_tag)
        else:
            clean_raw = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_") or "custom"
            scope_key = "%s%s" % (clean_raw, layer_tag)
        info.scope_key = scope_key
        info.label = _driver_display_name(s, info)
        return info

    attr, idx_str = s.driver_channel.split(":")
    idx = int(idx_str)
    if s.driver_obj is None:
        info = _make_rna_error("No driver loaded")
        info.scope_key = "obj_%s%s" % (s.driver_channel.replace(":", "_"), layer_tag)
        info.label = "No driver loaded"
        return info

    d_var_path = full_path(s.driver_bone, var_path(attr, idx))
    info = inspect_rna_target(s.driver_obj, d_var_path)
    b_name = s.driver_bone or s.driver_obj.name
    info.scope_key = "%s_%s%s" % (b_name, s.driver_channel.replace(":", "_"), layer_tag)
    info.label = _driver_display_name(s, info)
    return info


def resolve_driver_target(s):
    """Backwards-compatible 6-tuple wrapper around resolve_driver_info(s)."""
    info = resolve_driver_info(s)
    return info.var_id, info.bone, info.rel_attr, info.index, info.var_data_path, info.scope_key


def resolve_id_and_path(obj, path):
    """Determine the host ID block and relative path for driving."""
    info = inspect_rna_target(obj, path)
    if info.anim_id is not None:
        return info.anim_id, info.anim_path
    if path.startswith("data.shape_keys."):
        if obj and hasattr(obj, "data") and hasattr(obj.data, "shape_keys") and obj.data.shape_keys:
            return obj.data.shape_keys, path[len("data.shape_keys."):]
    return obj, path


def resolve_id_path_index(obj, path, index=-1):
    """Like resolve_id_and_path, but keeps an index embedded in `path` (e.g. location[0])."""
    info = inspect_rna_target(obj, path)
    if info.anim_id is not None:
        return info.anim_id, info.anim_path, (info.index if info.index >= 0 else index)
    id_block, rel = resolve_id_and_path(obj, path)
    return id_block, rel, index


def get_value(obj, path, index):
    id_block, rel_path, index = resolve_id_path_index(obj, path, index)
    value = id_block.path_resolve(rel_path)
    if index >= 0:
        value = value[index]
    return float(value)


def _coerce_value(cur, value):
    if isinstance(cur, bool):
        return bool(round(float(value)))
    if isinstance(cur, int):
        return int(round(float(value)))
    return float(value)


def set_by_path(obj, path, index, value):
    """Assign `value` to the property at `path` (handles any ID block, array, or ID property)."""
    id_block, rel_path, index = resolve_id_path_index(obj, path, index)
    tokens = _split_path(rel_path)
    if not tokens:
        raise ValueError("Empty data path")

    elem = None
    if tokens[-1][0] == "idx" and len(tokens) > 1:
        parent_val = id_block.path_resolve(rel_path[:tokens[-1][2]])
        if _is_array_like(parent_val):
            elem = tokens[-1][1]
            tokens = tokens[:-1]
    elif index >= 0:
        elem = index

    kind, name, start = tokens[-1]
    owner = id_block.path_resolve(rel_path[:start]) if start > 0 else id_block

    if kind == "attr":
        if elem is None:
            cur = getattr(owner, name)
            setattr(owner, name, _coerce_value(cur, value))
        else:
            arr = getattr(owner, name)
            arr[elem] = _coerce_value(arr[elem], value)
    elif kind == "key":
        key = bpy.utils.unescape_identifier(name)
        if elem is None:
            cur = owner[key]
            owner[key] = _coerce_value(cur, value)
        else:
            arr = owner[key]
            arr[elem] = _coerce_value(arr[elem], value)
    else:
        raise ValueError("Path '%s' ends in an index with nothing to index" % path)


def read_driver_value(s):
    """Current value of the driver property (raises on failure)."""
    info = resolve_driver_info(s)
    if not info.ok:
        raise LookupError(info.error or "No driver loaded")
    return info.value


def write_driver_value(s, value):
    """Write the driver property (raises on failure)."""
    info = resolve_driver_info(s)
    if not info.ok or info.var_id is None:
        raise LookupError(info.error or "No driver loaded")
    set_by_path(info.var_id, info.var_data_path, -1, value)
    try:
        info.var_id.update_tag()
        if info.anim_id and info.anim_id != info.var_id:
            info.anim_id.update_tag()
    except _RNA_ERRORS:
        pass


def identity_for(path):
    return 1.0 if path.endswith("_scale") or path.endswith("scale") else 0.0


def owner_of(item):
    """The Object or PoseBone whose base channels this item represents."""
    if getattr(item, "target_kind", "OBJECT") != "OBJECT":
        return None
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
        'Any RNA path, e.g. ["myprop"], location[2], or bpy.data.materials["Mat"].node_tree.nodes["Value"].outputs[0].default_value',
    ))
    return items


DRIVER_ITEMS = _build_driver_items()


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
    return [t.strip() for t in s.ch_custom.split(";") if t.strip()]


def enabled_channels(s):
    """{(attr, axis)} for the enabled Location / Rotation / Scale / Quaternion toggles."""
    out = {(DIRECT_ATTR[k], a) for k, a in transform_specs(s)}
    out.update(("rotation_quaternion", i) for i in quat_specs(s))
    return out


def has_object_targets(s):
    return any(it.target_kind == "OBJECT" for it in s.driven_items)


def has_bone_targets(s):
    return any(it.target_kind == "OBJECT" and it.bone for it in s.driven_items)


def capture_driver_rest(s):
    """Remember the driver's current value as its rest value."""
    try:
        s.driver_rest = read_driver_value(s)
    except _READ_ERRORS:
        pass


def _leaf_attr(path):
    """Last attribute of an RNA path.

    'pose.bones["A"].rotation_euler' -> 'rotation_euler'
    'location[0]' -> 'location'
    """
    text = path or ""
    m = _BONE_SUBPATH_RE.match(text)
    if m:
        text = m.group("dot") or ""
    base, idx = parse_custom(text) if text else ("", -1)
    return base if idx >= 0 else text


def auto_scrub_range(s):
    """Pick a usable Min/Max for the scrub slider from the driver channel and its current value."""
    info = resolve_driver_info(s)
    attr = _leaf_attr(info.anim_path or info.rel_attr)
    lo, hi = -1.0, 1.0
    if attr == "rotation_euler":
        lo, hi = -math.pi, math.pi
    elif attr == "scale":
        lo, hi = 0.0, 2.0
    elif attr == "location":
        lo, hi = -2.0, 2.0
    elif info.ok and info.value is not None:
        v = info.value
        if 0.0 <= v <= 1.0:
            lo, hi = 0.0, 1.0
        elif v > 1.0:
            lo, hi = 0.0, max(2.0, v * 2.0)
        elif v < -1.0:
            lo, hi = min(-2.0, v * 2.0), 0.0
    if info.ok and info.value is not None:
        lo, hi = min(lo, info.value), max(hi, info.value)
    s.scrub_min = lo
    s.scrub_max = hi


# --------------------------------------------------------------------------
# Context / Clipboard path extraction helpers
# --------------------------------------------------------------------------

def _normalize_copied_path(context, text):
    """Convert bpy.context... or relative Data Paths to bpy.data... when resolvable."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if _is_bpy_data_path(raw):
        return raw

    # Handle bpy.context.X or C.X
    ctx_prefixes = (
        ("bpy.context.active_pose_bone", lambda c: (c.active_object, 'pose.bones["%s"]' % bpy.utils.escape_identifier(c.active_pose_bone.name)) if c.active_object and c.active_pose_bone else (None, "")),
        ("bpy.context.active_object", lambda c: (c.active_object, "")),
        ("bpy.context.object", lambda c: (c.object, "")),
        ("bpy.context.scene", lambda c: (c.scene, "")),
        ("bpy.context.world", lambda c: (c.world, "")),
        ("bpy.context.material", lambda c: (getattr(c, "material", None), "")),
        ("C.active_object", lambda c: (c.active_object, "")),
        ("C.object", lambda c: (c.object, "")),
        ("C.scene", lambda c: (c.scene, "")),
    )
    for prefix, getter in ctx_prefixes:
        if raw.startswith(prefix) and (len(raw) == len(prefix) or raw[len(prefix)] in ".["):
            base_id, base_sub = getter(context)
            if base_id is not None:
                tail = raw[len(prefix):].lstrip(".")
                rel = _join_rel(base_sub, tail)
                return _join_full(repr(base_id), rel)

    # Probe active context ID blocks if user copied a relative path (Ctrl+Shift+C)
    candidates = []
    if context.active_object:
        obj = context.active_object
        if context.mode == "POSE" and context.active_pose_bone:
            candidates.append((obj, 'pose.bones["%s"]' % bpy.utils.escape_identifier(context.active_pose_bone.name)))
        candidates.append((obj, ""))
        if getattr(obj, "data", None):
            candidates.append((obj.data, ""))
            if getattr(obj.data, "shape_keys", None):
                candidates.append((obj.data.shape_keys, ""))
        if getattr(obj, "active_material", None):
            mat = obj.active_material
            candidates.append((mat, ""))
            if getattr(mat, "node_tree", None):
                candidates.append((mat, "node_tree"))
    if context.scene:
        candidates.append((context.scene, ""))
        if getattr(context.scene, "world", None):
            candidates.append((context.scene.world, ""))
            if getattr(context.scene.world, "node_tree", None):
                candidates.append((context.scene.world, "node_tree"))
        if getattr(context.scene, "node_tree", None):
            candidates.append((context.scene, "node_tree"))

    for cand_id, cand_sub in candidates:
        rel = _join_rel(cand_sub, raw)
        probe = _join_full(repr(cand_id), rel)
        info = inspect_rna_target(None, probe)
        if info.ok or info.array_len > 0:
            return probe

    return raw


def _find_embedded_owner_repr(id_block):
    """Map an embedded NodeTree back to 'bpy.data.materials[...].node_tree' etc."""
    if not getattr(id_block, "is_embedded_data", False):
        return repr(id_block)
    for coll in (bpy.data.materials, bpy.data.worlds, bpy.data.lights, bpy.data.scenes, bpy.data.linestyles):
        for owner in coll:
            if getattr(owner, "node_tree", None) == id_block:
                return repr(owner) + ".node_tree"
    return repr(id_block)


def extract_context_button_path(context):
    """Extract full RNA data path for the UI property currently under the right-click menu."""
    wm = context.window_manager
    if bpy.ops.ui.copy_data_path_button.poll():
        old_clip = wm.clipboard
        try:
            res = bpy.ops.ui.copy_data_path_button(full_path=True)
            copied = (wm.clipboard or "").strip()
        finally:
            wm.clipboard = old_clip
        if "FINISHED" in res and copied:
            return copied

    # Fallback for Blender 4.0+ context.property
    prop_tuple = getattr(context, "property", None)
    if prop_tuple is not None:
        ptr, prop_path, idx = prop_tuple
        if ptr is not None and getattr(ptr, "id_data", None) is not None and prop_path:
            id_repr = _find_embedded_owner_repr(ptr.id_data)
            prefix = ptr.path_from_id() if ptr != ptr.id_data else ""
            rel = _join_rel(prefix, prop_path)
            full = _join_full(id_repr, rel)
            if idx >= 0:
                full = "%s[%d]" % (full, idx)
            return full

    return ""


# --------------------------------------------------------------------------
# Dummy constraint target (hidden Empty) and its cleanup
# --------------------------------------------------------------------------

def _dummy_collection(scene):
    coll = bpy.data.collections.get(DUMMY_COLLECTION_NAME)
    if coll is None:
        coll = bpy.data.collections.new(DUMMY_COLLECTION_NAME)
        coll.hide_render = True
        coll.hide_viewport = True
    if scene is not None and scene.collection is not None:
        if coll.name not in scene.collection.children:
            scene.collection.children.link(coll)
    return coll


def get_or_create_dummy_target():
    """Ensure a dummy non-evaluating Empty object exists to satisfy Transformation / Action constraints."""
    scene = bpy.context.scene if bpy.context else (bpy.data.scenes[0] if bpy.data.scenes else None)
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        empty = bpy.data.objects.new(DUMMY_TARGET_NAME, None)
        empty.empty_display_size = 0.001
        empty.hide_render = True
        empty.hide_viewport = True
    if not empty.users_collection:
        _dummy_collection(scene).objects.link(empty)
    return empty


def _constraint_owners(obj):
    yield obj
    if obj.pose:
        for pb in obj.pose.bones:
            yield pb


def dummy_target_in_use():
    empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
    if empty is None:
        return False
    for ob in bpy.data.objects:
        if ob == empty:
            continue
        for owner in _constraint_owners(ob):
            for con in owner.constraints:
                if getattr(con, "target", None) == empty:
                    return True
    return False


def cleanup_dummy_target():
    """Remove SDK_Constraint_Target (and its hidden collection) when no constraint references it."""
    try:
        empty = bpy.data.objects.get(DUMMY_TARGET_NAME)
        if empty is None or dummy_target_in_use():
            return False
        bpy.data.objects.remove(empty, do_unlink=True)
        coll = bpy.data.collections.get(DUMMY_COLLECTION_NAME)
        if coll is not None and not coll.objects and not coll.children:
            bpy.data.collections.remove(coll)
        return True
    except _RNA_ERRORS:
        traceback.print_exc()
        return False


# --------------------------------------------------------------------------
# Driver / curve helpers (High performance O(1) lookups)
# --------------------------------------------------------------------------

def _is_array_property(target_id, path):
    """True if `path` resolves to an array (vector, list, ID-prop array)."""
    try:
        value = target_id.path_resolve(path)
    except _RNA_ERRORS:
        return False
    return _is_array_like(value)


def find_driver_fcurve(target_id, path, index):
    """Fast driver lookup using drivers.find(path, index)."""
    if target_id is None:
        return None
    ad = getattr(target_id, "animation_data", None)
    if ad is None:
        return None
    want = index if index >= 0 else 0
    try:
        fc = ad.drivers.find(path, index=want)
    except TypeError:
        fc = next((f for f in ad.drivers if f.data_path == path and f.array_index == want), None)
    if fc is not None and index < 0 and _is_array_property(target_id, path):
        return None
    return fc


def is_sdk_fcurve(fc):
    return any(v.name == SDK_VAR for v in fc.driver.variables)


def driver_matches(fc, drv_id, d_var_path):
    """True if the SDK f-curve is driven by exactly this driver property."""
    var = next((v for v in fc.driver.variables if v.name == SDK_VAR), None)
    if var is None:
        return False
    tgt = var.targets[0]
    return tgt.id == drv_id and tgt.data_path == d_var_path


def _safe_id_type(driver_target, id_block):
    valid = set(driver_target.bl_rna.properties["id_type"].enum_items.keys())
    t = getattr(id_block, "id_type", None)
    if t in valid:
        return t
    aliases = {"LIGHT": "LAMP", "COLLECTION": "GROUP"}
    if t in aliases and aliases[t] in valid:
        return aliases[t]
    rna_name = getattr(getattr(id_block, "rna_type", None), "name", "").upper().replace(" ", "")
    if rna_name in valid:
        return rna_name
    return "OBJECT"


def ensure_driver_on_id(target_id, rel_path, index, drv_id, drv_data_path, mute):
    """Return (fcurve, message) on an arbitrary ID datablock. fcurve is None if channel can't be driven."""
    if target_id is None:
        return None, "Target datablock not found"
    fc = find_driver_fcurve(target_id, rel_path, index)
    if fc is None:
        if index < 0 and _is_array_property(target_id, rel_path):
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        ad = getattr(target_id, "animation_data", None)
        before = {f.array_index for f in ad.drivers if f.data_path == rel_path} if ad else set()
        try:
            fc = target_id.driver_add(rel_path, index) if index >= 0 else target_id.driver_add(rel_path)
        except _RNA_ERRORS as e:
            return None, "Cannot drive '%s': %s" % (rel_path, e)
        if isinstance(fc, (list, tuple)):
            for f in fc:
                if f.array_index not in before:
                    try:
                        target_id.driver_remove(rel_path, f.array_index)
                    except _RNA_ERRORS:
                        pass
            return None, "'%s' is an array; give an index, e.g. %s[0]" % (rel_path, rel_path)
        fc.mute = mute
    elif not is_sdk_fcurve(fc):
        return None, ("'%s' already has a driver that wasn't made by this add-on "
                      "(remove or rename it first)" % var_path(rel_path, index))

    drv = fc.driver
    var = next((v for v in drv.variables if v.name == SDK_VAR), None)

    same = (
        var is not None
        and var.targets[0].id == drv_id
        and var.targets[0].data_path == drv_data_path
    )
    if not same:
        if var is not None and len(fc.keyframe_points):
            _warn("'%s' was re-bound to a different driver; its previous keys were discarded"
                  % var_path(rel_path, index))
        while len(fc.keyframe_points):
            fc.keyframe_points.remove(fc.keyframe_points[0], fast=True)
        for v in list(drv.variables):
            drv.variables.remove(v)
        var = drv.variables.new()
        var.name = SDK_VAR
        var.type = "SINGLE_PROP"
        t = var.targets[0]
        t.id_type = _safe_id_type(t, drv_id)
        t.id = drv_id
        t.data_path = drv_data_path
        drv.type = "SUM"

    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    return fc, ""


def ensure_driver(obj, path, index, drv_obj, drv_data_path, mute):
    """Wrapper resolving (obj, path) across ID boundaries before calling ensure_driver_on_id."""
    target_id, rel_path = resolve_id_and_path(obj, path)
    return ensure_driver_on_id(target_id, rel_path, index, drv_obj, drv_data_path, mute)


def key_curve(fc, x, y, interpolation, extrapolation):
    """Set the key at driver value `x` to `y` (replacing a key already there)."""
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


def add_rest_key(s, fc, x, rest_y, x_scale=1.0):
    """Key the rest state (driver at its rest value) before / together with the posed key."""
    if rest_y is None or not s.auto_rest_key:
        return False
    if abs(x - s.driver_rest) < KEY_EPS:
        return False
    kps = fc.keyframe_points
    n = len(kps)
    only_pending = n == 0 or (n == 1 and abs(kps[0].co.x - x * x_scale) < KEY_EPS)
    if not only_pending:
        return False
    key_curve(fc, s.driver_rest * x_scale, rest_y, s.interpolation, s.extrapolation)
    return True


def copy_curve_flipped(src, dst, sign):
    """Replace all keys of `dst` with those of `src`, values multiplied by `sign`."""
    kps = dst.keyframe_points
    while len(kps):
        kps.remove(kps[0], fast=True)
    auto = ("AUTO", "AUTO_CLAMPED")
    for kp in src.keyframe_points:
        key_curve(dst, kp.co.x, kp.co.y * sign, kp.interpolation, src.extrapolation)
        new = next((k for k in dst.keyframe_points if abs(k.co.x - kp.co.x) < KEY_EPS), None)
        if new is None:
            continue
        new.handle_left_type = kp.handle_left_type
        new.handle_right_type = kp.handle_right_type
        if kp.handle_left_type not in auto:
            new.handle_left = (kp.handle_left.x, kp.handle_left.y * sign)
        if kp.handle_right_type not in auto:
            new.handle_right = (kp.handle_right.x, kp.handle_right.y * sign)
    dst.update()


def safe_call(fn, label, *args, **kwargs):
    """Run one key operation. A failure becomes a message instead of aborting everything."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return "%s failed: %s" % (label, e)


def _fit_scope(scope_key, overhead):
    """Shorten `scope_key` so that overhead + scope fits Blender's 63-byte name limit."""
    room = MAX_ID_NAME - overhead
    if len(scope_key.encode("utf-8")) <= room:
        return scope_key
    tag = "~%08x" % (zlib.crc32(scope_key.encode("utf-8")) & 0xFFFFFFFF)
    head = scope_key
    while len(head.encode("utf-8")) > room - len(tag):
        head = head[:-1]
    return head + tag


# --------------------------------------------------------------------------
# SDK constraint predicates, Live Preview muting
# --------------------------------------------------------------------------

_SDK_TRANSFORM_PREFIXES = tuple(v + " [" for v in SDK_CON_BASE.values())
_SDK_NAME_TAIL_RE = re.compile(r"\](\.\d+)?$")


def is_sdk_transform_constraint(con):
    return (con.type == "TRANSFORM" and con.name.startswith(_SDK_TRANSFORM_PREFIXES)
            and _SDK_NAME_TAIL_RE.search(con.name) is not None)


def is_sdk_action_constraint(con):
    return (con.type == "ACTION" and con.name.startswith(SDK_ACTION_CON_PREFIX)
            and _SDK_NAME_TAIL_RE.search(con.name) is not None)


def find_sdk_constraint(owner, name, type_):
    """The constraint called `name`, or Blender's auto-renamed twin (`name.001`, `name.002`...)."""
    con = owner.constraints.get(name)
    if con is not None:
        return con
    twins = [c for c in owner.constraints
             if c.type == type_ and c.name.startswith(name + ".") and c.name[len(name) + 1:].isdigit()]
    return min(twins, key=lambda c: c.name) if twins else None


def is_sdk_constraint(con):
    return is_sdk_transform_constraint(con) or is_sdk_action_constraint(con)


def _sdk_id_blocks(obj):
    blocks = [obj]
    keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if keys is not None:
        blocks.append(keys)
    return blocks


def _mute_id_sdk_drivers(id_block, muted, seen_ids):
    if id_block is None:
        return
    ptr = id_block.as_pointer()
    if ptr in seen_ids:
        return
    seen_ids.add(ptr)
    ad = getattr(id_block, "animation_data", None)
    if ad:
        for fc in ad.drivers:
            if is_sdk_fcurve(fc):
                fc.mute = muted


def set_sdk_muted_for_settings(s, muted):
    """Mute / unmute every SDK driver and SDK constraint referenced by `s.driven_items`."""
    seen_ids = set()
    seen_objs = set()
    cspecs = custom_specs(s)
    for item in s.driven_items:
        if item.target_kind == "RNA_PATH":
            info = inspect_rna_target(None, item.rna_path)
            if info.anim_id is not None:
                _mute_id_sdk_drivers(info.anim_id, muted, seen_ids)
            continue

        obj = item.obj
        if obj is None:
            continue
        for block in _sdk_id_blocks(obj):
            _mute_id_sdk_drivers(block, muted, seen_ids)
        for rel in cspecs:
            info = inspect_rna_target(obj, rel, bone=item.bone)
            if info.anim_id is not None:
                _mute_id_sdk_drivers(info.anim_id, muted, seen_ids)
        if obj.as_pointer() not in seen_objs:
            seen_objs.add(obj.as_pointer())
            for owner in _constraint_owners(obj):
                for con in owner.constraints:
                    if is_sdk_constraint(con):
                        con.mute = muted


# --------------------------------------------------------------------------
# Rest values
# --------------------------------------------------------------------------

def capture(item, s=None):
    """Snapshot base channels / RNA values for an item."""
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.ok and info.value is not None:
            item.rest_rna = info.value
            item.has_rest_rna = True
        return

    t = owner_of(item)
    if t is None:
        return
    item.rest_loc = t.location
    item.rest_euler = t.rotation_euler
    item.rest_quat = t.rotation_quaternion
    item.rest_scale = t.scale
    if s is not None:
        capture_custom_rest(item, s)


def capture_custom_rest(item, s, only_missing=False):
    """Remember the current value of `ch_custom` driven channels for an OBJECT item."""
    if item.target_kind != "OBJECT":
        return
    obj = item.obj
    if obj is None:
        return
    try:
        store = json.loads(item.rest_custom or "{}")
    except ValueError:
        store = {}
    for rel in custom_specs(s):
        if only_missing and rel in store:
            continue
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.ok and info.value is not None:
            store[rel] = info.value
    item.rest_custom = json.dumps(store)


def rest_value_for(item, attr, idx, raw_key=None):
    """Rest value of a driven channel, or None if unknown."""
    if item.target_kind == "RNA_PATH":
        if item.has_rest_rna:
            return item.rest_rna
        if ".value" in item.rna_path and "key_blocks[" in item.rna_path:
            return 0.0
        return None

    if raw_key is None and idx >= 0:
        vec = {
            "location": item.rest_loc,
            "rotation_euler": item.rest_euler,
            "rotation_quaternion": item.rest_quat,
            "scale": item.rest_scale,
        }.get(attr)
        if vec is not None:
            return vec[idx]
    try:
        store = json.loads(item.rest_custom or "{}")
    except ValueError:
        store = {}
    for k in (raw_key, var_path(attr, idx)):
        if k and k in store:
            return float(store[k])
    if attr.endswith(".value") and "key_blocks[" in attr:
        return 0.0
    return None


# --------------------------------------------------------------------------
# Additive mode helpers (Multi-axis scoped constraints)
# --------------------------------------------------------------------------

def constraint_name_for(kind, scope_key):
    base = SDK_CON_BASE[kind]
    return "%s [%s]" % (base, _fit_scope(scope_key, len(base) + 3))


def actual_constraint_name(item, kind, scope_key):
    name = constraint_name_for(kind, scope_key)
    owner = owner_of(item)
    if owner is not None:
        con = find_sdk_constraint(owner, name, "TRANSFORM")
        if con is not None:
            return con.name
    return name


def additive_targets(item, kind, axis, scope_key):
    con_name = actual_constraint_name(item, kind, scope_key)
    a = "xyz"[axis]
    rel = con_data_path(con_name) + "."
    return [
        (full_path(item.bone, rel + "to_%s_%s%s" % (m, a, CON_SUFFIX[kind])), -1)
        for m in ("min", "max")
    ]


def ensure_constraint(item, kind, scope_key, mute):
    owner = owner_of(item)
    con_name = constraint_name_for(kind, scope_key)
    con = find_sdk_constraint(owner, con_name, "TRANSFORM")
    if con is None:
        con = owner.constraints.new("TRANSFORM")
        con.name = con_name
        if con.name != con_name:
            _warn("Blender renamed constraint '%s' to '%s' (name already used in the pose); using that name"
                  % (con_name, con.name))
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
            con.mix_mode = "ADD"
        elif kind == "rot":
            con.map_to = "ROTATION"
            con.mix_mode_rot = "ADD"
        else:
            con.map_to = "SCALE"
            con.mix_mode_scale = "MULTIPLY"
    con.mute = mute
    con.target = get_or_create_dummy_target()
    con.subtarget = ""
    return con


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


# --------------------------------------------------------------------------
# Mode conflict detection & Keying
# --------------------------------------------------------------------------

def mode_conflict(item, mode, attr, axis, scope_key=""):
    obj = item.obj
    owner = owner_of(item)
    if obj is None or owner is None or attr not in _CHANNEL_ATTRS or axis < 0:
        return ""
    label = "%s %s[%d]" % (item.bone or obj.name, attr, axis)
    path = full_path(item.bone, attr)

    if mode != "DIRECT":
        fc = find_driver_fcurve(obj, path, axis)
        if fc is not None and is_sdk_fcurve(fc):
            return "%s is already driven in Direct mode (use Delete Curves first)" % label

    if mode == "DIRECT" and attr in _ATTR_KIND:
        kind = _ATTR_KIND[attr]
        prefix = SDK_CON_BASE[kind] + " ["
        for con in owner.constraints:
            if not is_sdk_transform_constraint(con) or not con.name.startswith(prefix):
                continue
            for p, i in additive_targets(item, kind, axis, con.name[len(prefix):con.name.rfind("]")]):
                fc = find_driver_fcurve(obj, p, i)
                if fc is not None and is_sdk_fcurve(fc) and len(fc.keyframe_points):
                    return "%s already has the Additive SDK layer '%s' (use Delete Curves first)" % (label, con.name)

    if mode == "DIRECT":
        for con in owner.constraints:
            if not is_sdk_action_constraint(con) or con.action is None:
                continue
            try:
                found = action_find_fcurve(con.action, path, axis)
            except RuntimeError:
                break
            if found is not None:
                return "%s already has the Action SDK constraint '%s' (use Delete Curves first)" % (label, con.name)
    return ""


def key_additive(s, item, kind, axis, drv_info, scope_key, x):
    """Key one transform channel as an additive offset."""
    obj = item.obj
    owner = owner_of(item)
    if item.bone and obj == drv_info.var_id and item.bone == drv_info.bone:
        return "Bone '%s' can't drive itself" % item.bone

    conflict = mode_conflict(item, "ADDITIVE", DIRECT_ATTR[kind], axis, scope_key)
    if conflict:
        return conflict

    if kind == "loc":
        base_off = owner.location[axis] - item.rest_loc[axis]
    elif kind == "rot":
        mode = owner.rotation_mode
        if mode == "AXIS_ANGLE":
            return "Axis-angle rotation isn't supported"
        base_off = current_euler(owner)[axis] - rest_euler(item, owner)[axis]
        if mode == "QUATERNION":
            base_off = (base_off + math.pi) % (2.0 * math.pi) - math.pi
    else:
        rest = item.rest_scale[axis]
        if abs(rest) < 1e-8:
            return "Base scale is zero on %s, can't compute a ratio" % obj.name
        base_off = owner.scale[axis] / rest

    targets = additive_targets(item, kind, axis, scope_key)
    for path, p_idx in targets:
        if obj == drv_info.anim_id and path == drv_info.anim_path and p_idx == drv_info.index:
            return "'%s' can't drive itself" % path

    ident = 1.0 if kind == "scale" else 0.0
    existing = ident
    if s.live_preview:
        p0, i0 = targets[0]
        if find_driver_fcurve(obj, p0, i0) is not None:
            try:
                existing = get_value(obj, p0, i0)
            except _RNA_ERRORS:
                existing = ident
    value = existing * base_off if kind == "scale" else existing + base_off

    try:
        ensure_constraint(item, kind, scope_key, not s.live_preview)
    except _RNA_ERRORS as e:
        return "Can't set up constraint on '%s': %s" % (item.bone or obj.name, e)

    targets = additive_targets(item, kind, axis, scope_key)
    for path, p_idx in targets:
        fc, msg = ensure_driver_on_id(obj, path, p_idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
        if fc is None:
            return msg
        add_rest_key(s, fc, x, ident)
        key_curve(fc, x, value, s.interpolation, s.extrapolation)
    return ""


def key_direct(s, item, attr, idx, drv_info, x):
    """Maya-style direct channel drive for standard Object/Bone channels."""
    obj = item.obj
    path = full_path(item.bone, attr)
    if obj == drv_info.anim_id and path == drv_info.anim_path and idx == drv_info.index:
        return "'%s' can't drive itself" % var_path(path, idx)
    conflict = mode_conflict(item, "DIRECT", attr, idx)
    if conflict:
        return conflict
    try:
        y = get_value(obj, path, idx)
    except _RNA_ERRORS:
        return "Can't read '%s'" % var_path(path, idx)
    fc, msg = ensure_driver_on_id(obj, path, idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
    if fc is None:
        return msg

    add_rest_key(s, fc, x, rest_value_for(item, attr, idx))
    key_curve(fc, x, y, s.interpolation, s.extrapolation)
    return ""


def key_direct_rna(s, item, target_info, drv_info, x, raw_key=None):
    """Direct drive for any resolved RNA target (any ID block, custom prop, node, shape key, etc.)."""
    if not target_info.ok:
        return target_info.error or "Invalid RNA path"

    anim_id = target_info.anim_id
    anim_path = target_info.anim_path
    idx = target_info.index

    if anim_id == drv_info.anim_id and anim_path == drv_info.anim_path and idx == drv_info.index:
        return "'%s' can't drive itself" % target_info.var_data_path

    if item.target_kind == "OBJECT" and anim_id == item.obj:
        # Check conflict if user manually entered a transform channel via Custom
        raw_attr = anim_path
        if item.bone and anim_path.startswith('pose.bones['):
            m = _BONE_SUBPATH_RE.match(anim_path)
            if m and m.group("dot"):
                raw_attr = m.group("dot")
        conflict = mode_conflict(item, "DIRECT", raw_attr, idx)
        if conflict:
            return conflict

    fc, msg = ensure_driver_on_id(anim_id, anim_path, idx, drv_info.var_id, drv_info.var_data_path, not s.live_preview)
    if fc is None:
        return msg

    rest_y = rest_value_for(item, anim_path, idx, raw_key=raw_key)
    add_rest_key(s, fc, x, rest_y)
    key_curve(fc, x, target_info.value, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Action Constraint Mode Helpers
# --------------------------------------------------------------------------

def _is_layered_only(action):
    return getattr(action, "fcurves", None) is None and hasattr(action, "slots")


def _action_slot(action, create=False):
    slots = getattr(action, "slots", None)
    if slots is None:
        return None
    if len(slots):
        return slots[0]
    if create:
        return slots.new(id_type="OBJECT", name="SDK")
    return None


def _action_channelbag(action, create=False):
    slot = _action_slot(action, create)
    if slot is None:
        return None
    if not len(action.layers):
        if not create:
            return None
        action.layers.new(name="SDK")
    layer = action.layers[0]
    if not len(layer.strips):
        if not create:
            return None
        layer.strips.new(type="KEYFRAME")
    return layer.strips[0].channelbag(slot, ensure=create)


def action_fcurves(action, create=False):
    fcs = getattr(action, "fcurves", None)
    if fcs is not None:
        return fcs
    if not hasattr(action, "slots"):
        raise RuntimeError("Unsupported Blender version: Action exposes neither fcurves nor slots")
    bag = _action_channelbag(action, create)
    return bag.fcurves if bag is not None else ()


def action_find_fcurve(action, path, index):
    fcs = action_fcurves(action)
    return fcs.find(path, index=index) if hasattr(fcs, "find") else None


def action_transform_channel(drv_info):
    if isinstance(drv_info.var_id, bpy.types.Object) and 0 <= drv_info.index <= 2:
        # Check that attribute on the Object / PoseBone is directly location/rotation_euler/scale
        tail = drv_info.anim_path
        if drv_info.bone:
            m = _BONE_SUBPATH_RE.match(drv_info.anim_path)
            tail = (m.group("dot") or "") if m else ""
        if tail == "location":
            return ("LOCATION_X", "LOCATION_Y", "LOCATION_Z")[drv_info.index]
        if tail == "rotation_euler":
            return ("ROTATION_X", "ROTATION_Y", "ROTATION_Z")[drv_info.index]
        if tail == "scale":
            return ("SCALE_X", "SCALE_Y", "SCALE_Z")[drv_info.index]
    return None


def action_constraint_name_for(scope_key):
    return "%s%s]" % (SDK_ACTION_CON_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_CON_PREFIX) + 1))


def action_name_for(scope_key):
    return "%s%s" % (SDK_ACTION_PREFIX, _fit_scope(scope_key, len(SDK_ACTION_PREFIX)))


def get_action_for_driver(scope_key):
    act_name = action_name_for(scope_key)
    act = bpy.data.actions.get(act_name)
    if act is None:
        act = bpy.data.actions.new(act_name)
        if hasattr(act, "id_root"):
            try:
                act.id_root = "OBJECT"
            except (TypeError, AttributeError, ValueError):
                pass
        act.use_fake_user = True
    if _is_layered_only(act):
        action_fcurves(act, create=True)
    return act


def ensure_action_constraint(s, item, action, scope_key, drv_info):
    owner = owner_of(item)
    if owner is None:
        return None, "Target owner not found"
    channel = action_transform_channel(drv_info)
    con_name = action_constraint_name_for(scope_key)
    con = find_sdk_constraint(owner, con_name, "ACTION")
    created = con is None
    if created:
        con = owner.constraints.new("ACTION")
        con.name = con_name
        if con.name != con_name:
            _warn("Blender renamed constraint '%s' to '%s' (name already used in the pose); using that name"
                  % (con_name, con.name))
        con.target_space = "LOCAL"
        if hasattr(con, "mix_mode"):
            try:
                con.mix_mode = "BEFORE_SPLIT"
            except (TypeError, ValueError, AttributeError):
                pass
        if hasattr(con, "show_expanded"):
            con.show_expanded = False

    if channel is None and not hasattr(con, "use_eval_time"):
        if created:
            owner.constraints.remove(con)
        return None, ("This Blender version has no Action Constraint evaluation time; "
                      "use a Location / Rotation / Scale driver channel")

    con.action = action
    if hasattr(con, "action_slot"):
        slot = _action_slot(action, create=_is_layered_only(action))
        if slot is not None and con.action_slot != slot:
            try:
                con.action_slot = slot
            except _RNA_ERRORS:
                pass
    if channel is not None:
        if hasattr(con, "use_eval_time"):
            con.use_eval_time = False
        con.target = drv_info.var_id
        con.subtarget = drv_info.bone or ""
        con.transform_channel = channel
    else:
        con.use_eval_time = True
        con.target = get_or_create_dummy_target()
        con.subtarget = ""
    con.mute = not s.live_preview
    return con, ""


def _eval_time_path(item, con_name):
    return full_path(item.bone, con_data_path(con_name) + ".eval_time")


def set_eval_time_curve(item, con_name, drv_id, d_var_path, v_min, v_max, mute):
    fc, msg = ensure_driver_on_id(item.obj, _eval_time_path(item, con_name), -1, drv_id, d_var_path, mute)
    if fc is None:
        _warn(msg)
        return
    kps = fc.keyframe_points
    while len(kps):
        kps.remove(kps[0], fast=True)
    key_curve(fc, v_min, 0.0, "LINEAR", "CONSTANT")
    key_curve(fc, v_max, 1.0, "LINEAR", "CONSTANT")


def remove_eval_time_driver(item, con_name):
    obj = item.obj
    if obj is None:
        return
    path = _eval_time_path(item, con_name)
    fc = find_driver_fcurve(obj, path, -1)
    if fc is not None and is_sdk_fcurve(fc):
        try:
            obj.driver_remove(path)
        except _RNA_ERRORS:
            pass


def update_action_constraint_ranges(action, s, drv_info, extra_items=()):
    if not action:
        return
    try:
        fcs = action_fcurves(action)
    except RuntimeError:
        return
    points = [kp.co.x for fc in fcs for kp in fc.keyframe_points]
    if not points:
        return
    f_min = int(math.floor(min(points)))
    f_max = int(math.ceil(max(points)))
    if f_max - f_min < 1:
        f_max = f_min + int(ACTION_FRAMES_PER_UNIT)
    v_min = f_min / ACTION_FRAMES_PER_UNIT
    v_max = f_max / ACTION_FRAMES_PER_UNIT

    con_name = action_constraint_name_for(drv_info.scope_key)
    seen = set()
    for src in list(s.driven_items) + list(extra_items):
        if getattr(src, "target_kind", "OBJECT") != "OBJECT":
            continue
        obj = src.obj
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        owners = [("", obj)] + [(pb.name, pb) for pb in (obj.pose.bones if obj.pose else ())]
        for bone, owner in owners:
            con = find_sdk_constraint(owner, con_name, "ACTION")
            if con is None or con.action != action:
                continue
            con.frame_start = f_min
            con.frame_end = f_max
            if getattr(con, "use_eval_time", False):
                set_eval_time_curve(SimpleNamespace(obj=obj, bone=bone), con.name,
                                    drv_info.var_id, drv_info.var_data_path,
                                    v_min, v_max, not s.live_preview)
            else:
                k = math.degrees(1.0) if str(con.transform_channel).startswith("ROTATION") else 1.0
                con.min = v_min * k
                con.max = v_max * k


_QUAT_IDENT = (1.0, 0.0, 0.0, 0.0)


def action_channel_value(item, owner, attr, idx):
    if attr == "location":
        return owner.location[idx] - item.rest_loc[idx], 0.0
    if attr == "rotation_euler":
        if owner.rotation_mode == "AXIS_ANGLE":
            raise ValueError("Axis-angle rotation isn't supported")
        off = current_euler(owner)[idx] - rest_euler(item, owner)[idx]
        if owner.rotation_mode == "QUATERNION":
            off = (off + math.pi) % (2.0 * math.pi) - math.pi
        return off, 0.0
    if attr == "scale":
        rest = item.rest_scale[idx]
        if abs(rest) < 1e-8:
            raise ValueError("Base scale is zero on %s, can't compute a ratio" % (item.bone or item.obj.name))
        return owner.scale[idx] / rest, 1.0
    if attr == "rotation_quaternion":
        delta = owner.rotation_quaternion @ Quaternion(item.rest_quat).inverted()
        return delta[idx], _QUAT_IDENT[idx]
    raise ValueError("Unsupported channel '%s'" % attr)


def action_rotation_skip(item, wants_euler, other_enabled):
    owner = owner_of(item)
    quat_bone = owner.rotation_mode == "QUATERNION"
    if wants_euler == (not quat_bone):
        return None
    if other_enabled:
        return ""
    name = item.bone or item.obj.name
    if quat_bone:
        return "'%s' uses quaternion rotation: enable the Quat channels (Euler curves don't affect it)" % name
    return "'%s' uses Euler rotation: enable the Rotation channels (quaternion curves don't affect it)" % name


def new_action_fcurve(action, fcs, path, index, group=""):
    key = "group_name" if _is_layered_only(action) else "action_group"
    if group:
        try:
            return fcs.new(path, index=index, **{key: group})
        except TypeError:
            pass
    return fcs.new(path, index=index)


def key_action_mode(s, item, attr, idx, action, scope_key, drv_info, x):
    obj = item.obj
    if item.bone and obj == drv_info.var_id and item.bone == drv_info.bone:
        return "Bone '%s' can't drive itself" % item.bone
    conflict = mode_conflict(item, "ACTION", attr, idx, scope_key)
    if conflict:
        return conflict
    path = full_path(item.bone, attr)
    try:
        y, ident = action_channel_value(item, owner_of(item), attr, idx)
    except _RNA_ERRORS as e:
        return "Can't read '%s': %s" % (var_path(path, idx), e)

    con, msg = ensure_action_constraint(s, item, action, scope_key, drv_info)
    if con is None:
        return msg

    fcs = action_fcurves(action, create=True)
    fc = fcs.find(path, index=max(idx, 0))
    # With Live Preview on, the viewport pose already includes this curve. The RNA
    # channel does not, so add the current curve value back or the key drifts.
    if s.live_preview and fc is not None and len(fc.keyframe_points) and attr != "rotation_quaternion":
        try:
            existing = float(fc.evaluate(x * ACTION_FRAMES_PER_UNIT))
        except _RNA_ERRORS:
            existing = ident
        if attr == "scale":
            y *= existing
        else:
            y += existing
    if fc is None:
        fc = new_action_fcurve(action, fcs, path, max(idx, 0), item.bone or "Object Transforms")

    add_rest_key(s, fc, x, ident, ACTION_FRAMES_PER_UNIT)
    key_curve(fc, x * ACTION_FRAMES_PER_UNIT, y, s.interpolation, s.extrapolation)
    return ""


# --------------------------------------------------------------------------
# Query helpers
# --------------------------------------------------------------------------

def direct_resolved_targets(s, item, scope_key=""):
    """Yield (target_id, rel_path, index, is_constraint_prop) for every enabled target on `item`."""
    out = []
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, False))
        return out

    obj = item.obj
    if obj is None:
        return out

    for kind, axis in transform_specs(s):
        out.append((obj, full_path(item.bone, DIRECT_ATTR[kind]), axis, False))
        if scope_key:
            for p, p_idx in additive_targets(item, kind, axis, scope_key):
                out.append((obj, p, p_idx, True))
    for i in quat_specs(s):
        out.append((obj, full_path(item.bone, "rotation_quaternion"), i, False))
    for rel in custom_specs(s):
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, ".constraints[" in info.anim_path))
    return out


def custom_direct_resolved_targets(s, item):
    """Yield (target_id, rel_path, index, is_constraint_prop) for Direct-only custom/RNA targets."""
    out = []
    if item.target_kind == "RNA_PATH":
        info = inspect_rna_target(None, item.rna_path)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, False))
        return out

    obj = item.obj
    if obj is None:
        return out
    for rel in custom_specs(s):
        info = inspect_rna_target(obj, rel, bone=item.bone)
        if info.anim_id is not None and info.anim_path:
            out.append((info.anim_id, info.anim_path, info.index, ".constraints[" in info.anim_path))
    return out


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


# --------------------------------------------------------------------------
# Mirror helpers
# --------------------------------------------------------------------------

_FLIP_WORDS = {"l": "r", "r": "l", "left": "right", "right": "left"}
_FLIP_SUFFIX_RE = re.compile(r"(?P<sep>[._-])(?P<word>L|R|left|right)(?P<tail>\.\d+)?$", re.IGNORECASE)
_FLIP_PREFIX_RE = re.compile(r"^(?P<word>L|R|left|right)(?P<sep>[._-])", re.IGNORECASE)


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


_DEFAULT_MIRROR = {
    "location": (-1.0, 1.0, 1.0),
    "rotation_euler": (1.0, -1.0, -1.0),
    "rotation_quaternion": (1.0, 1.0, -1.0, -1.0),
    "scale": (1.0, 1.0, 1.0),
}


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


# --------------------------------------------------------------------------
# Configuration Data & Helpers
# --------------------------------------------------------------------------

def obj_ref(obj):
    if obj is None:
        return {}
    data = getattr(obj, "data", None)
    return {
        "name": obj.name,
        "data": data.name if data is not None else "",
        "lib": obj.library.filepath if obj.library else "",
    }


def resolve_config_object(pointer_obj, ref):
    if pointer_obj is not None:
        return pointer_obj, "pointer"
    name = ref.get("name", "")
    lib = ref.get("lib") or ""
    if name:
        if not lib:
            obj = bpy.data.objects.get(name)
        else:
            obj = next((o for o in bpy.data.objects
                        if o.name == name and o.library and o.library.filepath == lib), None)
        if obj is None:
            obj = bpy.data.objects.get(name)
        if obj is not None:
            return obj, "name"
    data_name = ref.get("data", "")
    if data_name:
        cands = [o for o in bpy.data.objects if o.data is not None and o.data.name == data_name]
        if len(cands) == 1:
            return cands[0], "data"
    return None, ""


def parse_driven_data(raw):
    """Normalise driven_data (supports RNA_PATH entries and legacy OBJECT entries)."""
    out = []
    for e in json.loads(raw or "[]"):
        if isinstance(e, dict):
            kind = e.get("kind") or ("RNA_PATH" if e.get("rna_path") else "OBJECT")
            out.append({
                "kind": kind,
                "obj": e.get("obj") or {},
                "bone": e.get("bone", ""),
                "rna_path": e.get("rna_path", ""),
            })
        elif isinstance(e, (list, tuple)) and len(e) >= 2:
            out.append({"kind": "OBJECT", "obj": {"name": e[0]}, "bone": e[1], "rna_path": ""})
    return out


def _ref_label(ref, bone):
    return (ref.get("name") or "?") + ((":" + bone) if bone else "")


def _bools(vals, n):
    vals = [bool(v) for v in (vals or [])][:n]
    return tuple(vals + [False] * (n - len(vals)))


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
            drv_ref = json.loads(cfg.driver_ref or "{}")
        except ValueError:
            drv_ref = {}
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
        s.driver_channel = cfg.driver_channel
        s.driver_custom = cfg.driver_custom
        s.driver_rest = cfg.driver_rest
        s.mode = cfg.mode
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

        invalidate_key_cache()
        self.report({"INFO"}, "Loaded configuration '%s'" % cfg.name)
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


def _config_to_dict(cfg):
    try:
        drv_ref = json.loads(cfg.driver_ref or "{}")
    except ValueError:
        drv_ref = {}
    if cfg.driver_obj is not None:
        drv_ref = obj_ref(cfg.driver_obj)
    driven = []
    try:
        entries = parse_driven_data(cfg.driven_data)
    except (ValueError, TypeError):
        entries = []
    for i, entry in enumerate(entries):
        if entry["kind"] == "RNA_PATH":
            driven.append({"kind": "RNA_PATH", "rna_path": entry["rna_path"], "obj": {}, "bone": ""})
        else:
            ptr = cfg.driven_refs[i].obj if i < len(cfg.driven_refs) else None
            driven.append({
                "kind": "OBJECT",
                "obj": obj_ref(ptr) if ptr is not None else entry["obj"],
                "bone": entry["bone"],
                "rna_path": "",
            })
    return {
        "name": cfg.name,
        "layer_tag": cfg.config_layer_tag,
        "mode": cfg.mode,
        "driver": {
            "obj": drv_ref, "bone": cfg.driver_bone, "channel": cfg.driver_channel,
            "custom": cfg.driver_custom, "rest": cfg.driver_rest,
        },
        "channels": {
            "loc": list(cfg.ch_loc), "rot": list(cfg.ch_rot), "quat": list(cfg.ch_quat),
            "scale": list(cfg.ch_scale), "custom": cfg.ch_custom,
        },
        "driven": driven,
    }


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
            "version": list(bl_info["version"]),
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
        for c in items:
            try:
                drv = c.get("driver", {})
                ch = c.get("channels", {})
                cfg = s.configs.add()
                cfg.name = c.get("name", "Imported_SDK_Config")
                cfg.config_layer_tag = c.get("layer_tag", "")
                cfg.mode = c.get("mode", "ADDITIVE")
                drv_ref = drv.get("obj") or {}
                cfg.driver_ref = json.dumps(drv_ref)
                obj, _how = resolve_config_object(None, drv_ref)
                cfg.driver_obj = obj
                uses_abs = (drv.get("channel") == "CUSTOM" and _is_bpy_data_path(drv.get("custom", "")))
                if not uses_abs and obj is None and drv_ref:
                    missing.append("driver object '%s'" % (drv_ref.get("name") or "?"))
                cfg.driver_bone = drv.get("bone", "")
                cfg.driver_channel = drv.get("channel", "location:0")
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
                        ref = d.get("obj") or {}
                        entries.append({"kind": "OBJECT", "obj": ref, "bone": d.get("bone", ""), "rna_path": ""})
                        d_obj, _how = resolve_config_object(None, ref)
                        cfg.driven_refs.add().obj = d_obj
                        if d_obj is None:
                            missing.append(_ref_label(ref, d.get("bone", "")))
                cfg.driven_data = json.dumps(entries)
                added += 1
            except (AttributeError, TypeError, ValueError, KeyError) as e:
                self.report({"WARNING"}, "Skipped a malformed configuration: %s" % e)
        if added:
            s.config_index = len(s.configs) - 1
        self.report({"INFO"}, "Imported %d configuration(s)" % added)
        if missing:
            self.report({"WARNING"}, "Not found in this file (kept by path/name): %s"
                        % (", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")))
        return {"FINISHED"} if added else {"CANCELLED"}


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
        invalidate_key_cache()
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


def _remove_driver_target(target_id, rel_path, p_idx, is_con):
    fc = find_driver_fcurve(target_id, rel_path, p_idx)
    if fc is None or not is_sdk_fcurve(fc):
        return False
    try:
        target_id.driver_remove(rel_path, p_idx) if p_idx >= 0 else target_id.driver_remove(rel_path)
    except _RNA_ERRORS:
        return False
    if is_con and ".constraints[" in rel_path:
        try:
            set_by_path(target_id, rel_path, p_idx, identity_for(rel_path))
        except _RNA_ERRORS:
            pass
    return True


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


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

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


def _context_menu_type():
    return getattr(bpy.types, "UI_MT_button_context_menu", None) or getattr(bpy.types, "WM_MT_button_context", None)


def register():
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except ValueError:
            # Already registered (reload or dual legacy + extension install)
            pass
    if not hasattr(bpy.types.Scene, "sdk_settings"):
        bpy.types.Scene.sdk_settings = PointerProperty(type=SDK_Settings)
    for handlers in _HANDLER_LISTS:
        if _on_history_change not in handlers:
            handlers.append(_on_history_change)
    mt = _context_menu_type()
    if mt is not None:
        # Safe to append more than once on most Blender builds; remove is idempotent in unregister
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


if __name__ == "__main__":
    register()
