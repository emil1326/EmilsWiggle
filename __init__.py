# Emil's Wiggle, a fork of Wiggle 2 by Steve Miller
# https://github.com/shteeve3d/blender-wiggle-2
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See LICENSE in this folder.

bl_info = {
    "name": "Emil's Wiggle",
    "author": "Emil (fork of Wiggle 2 by Steve Miller)",
    "version": (1, 2, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Emil",
    "description": "Spring physics on bones that works live and in renders, no baking needed",
    "doc_url": "https://github.com/shteeve3d/blender-wiggle-2",
    "category": "Animation",
}

if "bpy" in locals():
    import importlib
    for _mod in (solver, runtime, background, props, handlers, operators, debug, legacy, ui):  # noqa: F821
        importlib.reload(_mod)

import bpy

from . import solver, runtime, background, props, handlers, operators, debug, legacy, ui


def _detect_later():
    try:
        runtime.detect_legacy()
    except Exception:
        pass
    return None


def register():
    props.register()
    operators.register()
    debug.register()
    ui.register()
    handlers.register()
    legacy.register()
    background.register()
    bpy.app.timers.register(_detect_later, first_interval=0.5)


def unregister():
    background.unregister()
    legacy.unregister()
    handlers.unregister()
    # This also runs when Blender quits and on Reload Scripts, so don't delete anything here,
    # just take the wiggle off. Turning a scene off is what removes the constraints and empties.
    try:
        runtime.neutralize_helpers()
    except Exception:
        pass
    runtime.clear_all()
    ui.unregister()
    debug.unregister()
    operators.unregister()
    props.unregister()
