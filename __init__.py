# SPDX-License-Identifier: GPL-3.0-or-later
"""Set Driven Key — Maya-style driven keys for Blender (extension entry point)."""

from . import set_driven_key

# Re-export for convenience / legacy scripts that import the package.
bl_info = getattr(set_driven_key, "bl_info", None)


def register():
    set_driven_key.register()


def unregister():
    set_driven_key.unregister()
