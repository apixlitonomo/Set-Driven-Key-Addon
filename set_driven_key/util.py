"""Set Driven Key — util."""

import traceback
import zlib

from .const import MAX_ID_NAME, _WARNINGS



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
