"""
Emil's Wiggle - developer stuff, hidden unless "Developer Tools" is on in the add-on preferences.

The report is plain text meant to be pasted into a bug report / chat.
"""

import faulthandler
import os
import tempfile
import time

import bpy
from bpy.props import BoolProperty, IntProperty

from . import handlers, runtime

force_show = False  # tests draw the debug panel without an installed add-on

CRASH_LOG = os.path.join(tempfile.gettempdir(), "emils_wiggle_crash.log")
_crash_file = None


def enable_crash_log():
    """If Blender dies, write what every Python thread was doing to CRASH_LOG."""
    global _crash_file
    if _crash_file is not None or faulthandler.is_enabled():
        return
    try:
        if os.path.exists(CRASH_LOG) and os.path.getsize(CRASH_LOG) > 512_000:
            with open(CRASH_LOG, "rb") as f:
                f.seek(-256_000, os.SEEK_END)
                keep = f.read()
            with open(CRASH_LOG, "wb") as f:
                f.write(keep)
        _crash_file = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
        _crash_file.write(f"\n=== Blender {bpy.app.version_string} started {time.ctime()}"
                          f" (pid {os.getpid()}) ===\n")
        faulthandler.enable(_crash_file, all_threads=True)
    except Exception:
        _crash_file = None


def disable_crash_log():
    global _crash_file
    if _crash_file is None:
        return
    try:
        faulthandler.disable()
        _crash_file.close()
    except Exception:
        pass
    _crash_file = None


def _update_crash_log(self, context):
    if self.crash_log:
        enable_crash_log()
    else:
        disable_crash_log()


class EmilsWigglePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    show_debug: BoolProperty(
        name="Developer Tools",
        description="Show a Debug panel with counters and a button that copies a debug report",
        default=False)
    background_cache: BoolProperty(
        name="Background Cache",
        description="When you don't touch anything for a while, quietly simulate the frames that aren't "
                    "cached yet. It works in short slices and stops as soon as you do something",
        default=True)
    background_delay: IntProperty(
        name="Start After",
        description="Seconds without doing anything before the background cache starts",
        default=15, min=2, max=600)
    crash_log: BoolProperty(
        name="Crash Log",
        description="If Blender crashes, write what Python was doing at that moment to a log file "
                    "in the temp folder (costs nothing until a crash)",
        default=True, update=_update_crash_log)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.prop(self, "background_cache")
        sub = row.row()
        sub.enabled = self.background_cache
        sub.prop(self, "background_delay", text="After (s)")
        col.prop(self, "show_debug")
        col.prop(self, "crash_log")
        if self.crash_log:
            col.label(text=CRASH_LOG, icon="FILE_TEXT")


def preferences(context):
    addon = getattr(context, "preferences", bpy.context.preferences).addons.get(__package__)
    return addon.preferences if addon is not None else None


_prefs = preferences


def debug_enabled(context):
    if force_show:
        return True
    prefs = _prefs(context)
    return prefs is not None and prefs.show_debug


def _apply_crash_log_pref():
    prefs = _prefs(bpy.context)
    if prefs is not None and prefs.crash_log:
        enable_crash_log()
    return None


def _yes(value):
    return "yes" if value else "no"


def _n(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _side_text(side):
    if side.per_axis:
        values = "stiff %s damp %s grav %s" % tuple(
            "(" + " ".join(f"{v:g}" for v in axis) + ")"
            for axis in (side.stiff_axis, side.damp_axis, side.gravity_axis))
    else:
        values = f"stiff {side.stiff:g} damp {side.damp:g} grav {side.gravity:g}"
    text = f"mass {side.mass:g} {values} stretch {side.stretch:g}"
    locks = "".join(axis for axis, on in zip("XYZ", side.lock) if on)
    if locks:
        text += f" lock {locks}"
    if not side.chain:
        text += " no-chain"
    if side.wind_ob is not None:
        text += f" wind {side.wind_ob.name} x{side.wind:g}"
    if side.collider_type == "Object" and side.collider is not None:
        text += f" collider {side.collider.name} r{side.radius:g}"
    elif side.collider_type == "Collection" and side.collider_collection is not None:
        text += f" colliders [{side.collider_collection.name}] r{side.radius:g}"
    return text


def build_report(context):
    from . import bl_info
    scene = context.scene
    s = scene.emils_wiggle
    r = scene.render
    rt = runtime.peek(scene)
    start, end = runtime.playback_range(scene)
    lines = [
        "Emil's Wiggle debug report",
        f"add-on {'.'.join(map(str, bl_info['version']))} | Blender {bpy.app.version_string}"
        f" | file {bpy.path.basename(bpy.data.filepath) or '(unsaved)'}",
        f"scene {scene.name}: enabled {_yes(s.enabled)}, frame {scene.frame_current},"
        f" range {start}-{end}{' (preview)' if scene.use_preview_range else ''},"
        f" fps {r.fps}/{r.fps_base:g} = {r.fps / r.fps_base:.3f}",
        f"quality {s.iterations}, substeps {s.substeps}, preroll {s.preroll}, loop {_yes(s.loop)},"
        f" fast preview {_yes(s.fast_preview)}, cache {_yes(s.use_cache)} (locked {_yes(s.cache_locked)}),"
        f" gravity {tuple(round(v, 3) for v in scene.gravity)} (on {_yes(scene.use_gravity)})",
        f"lock interface {_yes(r.use_lock_interface)}, Wiggle 2 on {_yes(getattr(scene, 'wiggle_enable', False))},"
        f" playing {_yes(runtime._is_playing())}, rendering {_yes(rt is not None and rt.rendering)}",
    ]
    count, first, last = runtime.cache_info(scene)
    if count:
        lines.append(f"cache {_n(count, 'frame')} ({first}-{last}), this frame cached"
                     f" {_yes(runtime.frame_is_cached(scene, scene.frame_current))}")
    else:
        lines.append("cache empty")
    if rt is None:
        lines.append("no runtime for this scene yet")
    else:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(rt.counts.items())) or "none"
        lines.append(f"counters: {counts}")
        lines.append(f"last step {rt.stats_sim_ms:.2f} ms, handler errors {handlers.error_count},"
                     f" setup pending {_yes(rt.needs_edit)}, blocked bad empty values {runtime.bad_writes}")
        if rt.frame_ms > 0.0:
            lines.append(f"last playback {1000.0 / rt.frame_ms:.1f} fps ({rt.frame_ms:.0f} ms a frame)")
        from . import background
        enabled, delay = background._settings()
        lines.append(f"background cache {'on' if enabled else 'off'} after {delay:g}s: "
                     f"{background.status(scene) or 'idle'}, {background.stats}")
        if background.last_error:
            lines.append("background cache error: " + background.last_error.strip().splitlines()[-1])
        lines.append("rigs:")
        for rig in rt.rigs.values():
            tails = sum(b.has_tail for b in rig.bones)
            heads = sum(b.has_head for b in rig.bones)
            lines.append(
                f"  {rig.name}: {_n(len(rig.bones), 'bone')} ({tails} tail, {heads} head), ready {_yes(rig.ready)},"
                f" last frame {rig.last_frame}, cached {len(rig.cache)}, approx {_yes(rig.approx)},"
                f" fast preview {'ok' if rig.fast_ok else 'no (' + rig.fast_reason + ')'},"
                f" settings animated {_yes(rig.settings_animated)},"
                f" colliders {sorted(rig.colliders) or '-'}, winds {sorted(rig.winds) or '-'}")
            ob = scene.objects.get(rig.name)
            for b in rig.bones:
                pb = ob.pose.bones.get(b.name) if ob is not None else None
                if pb is None:
                    lines.append(f"    {b.name}: missing")
                    continue
                bone = pb.bone
                extra = []
                if b.helper is None:
                    extra.append("NO HELPER")
                others = [c.type for c in pb.constraints if c.name != runtime.CONSTRAINT_NAME]
                if others:
                    extra.append("constraints " + "/".join(others))
                if bone.inherit_scale != "FULL" or not bone.use_inherit_rotation:
                    extra.append(f"inherit rot {_yes(bone.use_inherit_rotation)} scale {bone.inherit_scale}")
                parent = b.parent.name if b.parent is not None else "-"
                lines.append(f"    {b.name} (wiggle parent {parent}, len {b.length:.3f}"
                             f"{', ' + ', '.join(extra) if extra else ''})")
                s_b = pb.emils_wiggle
                if b.has_tail:
                    lines.append(f"      tail: {_side_text(s_b.tail)}")
                if b.has_head:
                    lines.append(f"      head: {_side_text(s_b.head)}")
    lines.append("recent frames:")
    runs = []  # consecutive frames that did the same thing get one line
    for frame, rig_name, what in runtime.history:
        if runs and runs[-1][2:] == [rig_name, what] and frame == runs[-1][1] + 1:
            runs[-1][1] = frame
        else:
            runs.append([frame, frame, rig_name, what])
    for first_frame, last_frame, rig_name, what in runs[-20:]:
        span = str(first_frame) if first_frame == last_frame else f"{first_frame}-{last_frame}"
        lines.append(f"  {span:>9} {rig_name}: {what}")
    lines.append("last error:")
    lines.append(runtime.last_error.rstrip() or "  none")
    size = os.path.getsize(CRASH_LOG) if os.path.exists(CRASH_LOG) else 0
    lines.append(f"crash log: {CRASH_LOG} ({'on' if _crash_file else 'off'}, {size // 1024} KB)")
    return "\n".join(lines)


class EMILSWIGGLE_OT_copy_report(bpy.types.Operator):
    """Copy a text report about the wiggle setup and what it's been doing"""
    bl_idname = "emils_wiggle.copy_report"
    bl_label = "Copy Debug Report"

    def execute(self, context):
        text = build_report(context)
        context.window_manager.clipboard = text
        self.report({"INFO"}, f"Debug report copied ({len(text.splitlines())} lines)")
        return {"FINISHED"}


class EMILSWIGGLE_OT_open_crash_log(bpy.types.Operator):
    """Open the crash log file"""
    bl_idname = "emils_wiggle.open_crash_log"
    bl_label = "Open Crash Log"

    @classmethod
    def poll(cls, context):
        return os.path.exists(CRASH_LOG)

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=CRASH_LOG)
        return {"FINISHED"}


class EMILSWIGGLE_OT_reset_counters(bpy.types.Operator):
    """Reset the debug counters and the recent frame list"""
    bl_idname = "emils_wiggle.reset_counters"
    bl_label = "Reset Counters"

    def execute(self, context):
        rt = runtime.peek(context.scene)
        if rt is not None:
            rt.counts.clear()
        runtime.history.clear()
        runtime.last_error = ""
        handlers.error_count = 0
        return {"FINISHED"}


classes = (
    EmilsWigglePreferences,
    EMILSWIGGLE_OT_copy_report,
    EMILSWIGGLE_OT_open_crash_log,
    EMILSWIGGLE_OT_reset_counters,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    # the preferences of an add-on can't be read yet while it registers
    bpy.app.timers.register(_apply_crash_log_pref, first_interval=0.2)


def unregister():
    disable_crash_log()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
