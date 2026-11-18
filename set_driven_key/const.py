"""Set Driven Key — const."""

import re


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


# --------------------------------------------------------------------------
# SDK constraint predicates, Live Preview muting
# --------------------------------------------------------------------------

_SDK_TRANSFORM_PREFIXES = tuple(v + " [" for v in SDK_CON_BASE.values())
_SDK_NAME_TAIL_RE = re.compile(r"\](\.\d+)?$")


_QUAT_IDENT = (1.0, 0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# Mirror helpers
# --------------------------------------------------------------------------

_FLIP_WORDS = {"l": "r", "r": "l", "left": "right", "right": "left"}
_FLIP_SUFFIX_RE = re.compile(r"(?P<sep>[._-])(?P<word>L|R|left|right)(?P<tail>\.\d+)?$", re.IGNORECASE)
_FLIP_PREFIX_RE = re.compile(r"^(?P<word>L|R|left|right)(?P<sep>[._-])", re.IGNORECASE)


_DEFAULT_MIRROR = {
    "location": (-1.0, 1.0, 1.0),
    "rotation_euler": (1.0, -1.0, -1.0),
    "rotation_quaternion": (1.0, 1.0, -1.0, -1.0),
    "scale": (1.0, 1.0, 1.0),
}
