"""
Emil's Wiggle - app handlers.

frame_change_pre/post also run on the render thread during F12 / Ctrl+F12
(once per view layer), so they only use the scene and depsgraph they're given.
"""

import traceback

import bpy
from bpy.app.handlers import persistent

from . import runtime

error_count = 0


def _safe(fn, *args):
    global error_count
    try:
        fn(*args)
    except Exception:
        error_count += 1
        print("Emil's Wiggle: error in handler")
        traceback.print_exc()


@persistent
def emils_wiggle_frame_pre(scene, depsgraph=None):
    _safe(runtime.frame_pre, scene)


@persistent
def emils_wiggle_frame_post(scene, depsgraph=None):
    _safe(runtime.frame_post, scene, depsgraph)


@persistent
def emils_wiggle_depsgraph_post(scene, depsgraph=None):
    if depsgraph is not None:
        _safe(runtime.on_depsgraph_update, scene, depsgraph)


@persistent
def emils_wiggle_load_post(*_args):
    runtime.clear_all()
    _safe(runtime.detect_legacy)


@persistent
def emils_wiggle_undo_post(*_args):
    _safe(runtime.after_undo)


@persistent
def emils_wiggle_render_init(scene, *_args):
    _safe(runtime.render_started, scene)


@persistent
def emils_wiggle_playback_post(scene, *_args):
    _safe(runtime.playback_stopped, scene)


@persistent
def emils_wiggle_render_done(scene, *_args):
    _safe(runtime.render_finished, scene)


_HANDLERS = (
    ("frame_change_pre", emils_wiggle_frame_pre),
    ("frame_change_post", emils_wiggle_frame_post),
    ("depsgraph_update_post", emils_wiggle_depsgraph_post),
    ("load_post", emils_wiggle_load_post),
    ("undo_post", emils_wiggle_undo_post),
    ("redo_post", emils_wiggle_undo_post),
    ("render_init", emils_wiggle_render_init),
    ("render_complete", emils_wiggle_render_done),
    ("render_cancel", emils_wiggle_render_done),
    ("animation_playback_post", emils_wiggle_playback_post),
)


def _remove_ours(handler_list):
    for fn in list(handler_list):
        if getattr(fn, "__name__", "").startswith("emils_wiggle_"):
            handler_list.remove(fn)


def register():
    for name, fn in _HANDLERS:
        lst = getattr(bpy.app.handlers, name, None)
        if lst is None:
            continue
        _remove_ours(lst)
    for name, fn in _HANDLERS:
        lst = getattr(bpy.app.handlers, name, None)
        if lst is not None:
            lst.append(fn)


def unregister():
    for name, _fn in _HANDLERS:
        lst = getattr(bpy.app.handlers, name, None)
        if lst is not None:
            _remove_ours(lst)
