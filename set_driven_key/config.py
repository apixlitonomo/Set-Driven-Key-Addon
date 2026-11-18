"""Set Driven Key — config."""

import json

import bpy



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
