"""
Emil's Wiggle - cleaning up after Wiggle 2.

Wiggle 2's unregister() removes its PropertyGroup classes but leaves the
Scene/Object/PoseBone "wiggle" pointer properties behind. Disable Wiggle 2 while
Blender runs and those properties point at freed types: anything that walks them
later (resolving a property path for the Info log, for example) can crash Blender.
A slow timer watches for that and removes the leftovers.
"""

import bpy

WIGGLE2_POINTERS = (
    ("Scene", "wiggle", "WiggleScene"),
    ("Object", "wiggle", "WiggleObject"),
    ("PoseBone", "wiggle", "WiggleBone"),
)
CHECK_EVERY = 1.0


def wiggle2_active():
    """Wiggle 2 is enabled right now (its panel class is registered)."""
    return hasattr(bpy.types, "WIGGLE_PT_Settings")


def clean_dangling_wiggle2():
    removed = []
    for owner_name, prop, type_name in WIGGLE2_POINTERS:
        owner = getattr(bpy.types, owner_name)
        if prop in owner.bl_rna.properties and not hasattr(bpy.types, type_name):
            try:
                delattr(owner, prop)
                removed.append(f"{owner_name}.{prop}")
            except Exception:
                pass
    if removed:
        print("Emil's Wiggle: removed properties Wiggle 2 left behind:", ", ".join(removed))
    return removed


def _watch():
    try:
        clean_dangling_wiggle2()
    except Exception:
        pass
    return CHECK_EVERY


def register():
    if not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=CHECK_EVERY, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
