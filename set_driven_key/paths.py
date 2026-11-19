"""Set Driven Key — paths."""

from types import SimpleNamespace

import bpy

from .const import _BONE_SUBPATH_RE, _BPY_DATA_HEAD_RE, _CUSTOM_RE, _PATH_TOKEN_RE, _READ_ERRORS, _RNA_ERRORS



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


def identity_for(path):
    return 1.0 if path.endswith("_scale") or path.endswith("scale") else 0.0


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
# Driver / curve helpers (High performance O(1) lookups)
# --------------------------------------------------------------------------

def _is_array_property(target_id, path):
    """True if `path` resolves to an array (vector, list, ID-prop array)."""
    try:
        value = target_id.path_resolve(path)
    except _RNA_ERRORS:
        return False
    return _is_array_like(value)
