"""Set Driven Key — dummy."""

import traceback

import bpy

from .const import DUMMY_COLLECTION_NAME, DUMMY_TARGET_NAME, _RNA_ERRORS



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
