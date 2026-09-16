"""
Emil's Wiggle - cleaning up after Wiggle 2.

Wiggle 2's unregister() removes its PropertyGroup classes but leaves the
Scene/Object/PoseBone "wiggle" pointer properties behind. Disable Wiggle 2 while
Blender runs and those properties point at freed types: anything that walks them
later (resolving a property path for the Info log, for example) can crash Blender.

Wiggle 2's unregister() gets wrapped so the leftovers go away in the same call,
before Blender draws anything. They also go when Emil's Wiggle gets enabled, before
every save (saving a file with library overrides walks every property, and a dangling
one crashed Blender in the stress test) and after loading a file. A slow timer is the
backup (and wraps a Wiggle 2 that gets enabled or reloaded later).
"""

import sys

import bpy

WIGGLE2_POINTERS = (
    ("Scene", "wiggle", "WiggleScene"),
    ("Object", "wiggle", "WiggleObject"),
    ("PoseBone", "wiggle", "WiggleBone"),
)
WIGGLE2_NAME = "Wiggle 2"
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


def _wiggle2_modules():
    for mod in list(sys.modules.values()):
        try:
            names = vars(mod)  # not getattr: some modules import things lazily on attribute access
        except TypeError:
            continue
        info = names.get("bl_info")
        if isinstance(info, dict) and info.get("name") == WIGGLE2_NAME and callable(names.get("unregister")):
            yield mod


def hook_wiggle2():
    """Wrap Wiggle 2's unregister() so it cleans up after itself."""
    for mod in _wiggle2_modules():
        original = mod.unregister
        if getattr(original, "emils_wiggle_original", None) is not None:
            continue

        def unregister(_original=original):
            try:
                _original()
            finally:
                try:
                    clean_dangling_wiggle2()
                except Exception:
                    pass

        unregister.emils_wiggle_original = original
        mod.unregister = unregister


def unhook_wiggle2():
    for mod in _wiggle2_modules():
        original = getattr(mod.unregister, "emils_wiggle_original", None)
        if original is not None:
            mod.unregister = original


def _watch():
    try:
        clean_dangling_wiggle2()
        hook_wiggle2()
    except Exception:
        pass
    return CHECK_EVERY


def register():
    try:
        clean_dangling_wiggle2()
        hook_wiggle2()
    except Exception:
        pass
    if not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=CHECK_EVERY, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    try:
        unhook_wiggle2()
    except Exception:
        pass
