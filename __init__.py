# SPDX-License-Identifier: GPL-3.0-or-later
"""Set Driven Key — Maya-style driven keys for Blender (entry point).

The literal ``bl_info`` below is required by Blender 3.0–4.1 (it reads it without
importing the package). Keep it in sync with ``set_driven_key/__init__.py`` and
``blender_manifest.toml``.
"""

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

from . import set_driven_key


def register():
    set_driven_key.register()


def unregister():
    set_driven_key.unregister()
