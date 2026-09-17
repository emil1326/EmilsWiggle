"""
Emil's Wiggle - background cache.

When nothing has happened for a while (no edits, no frame changes, no redraws, nothing
playing or rendering), the frames of the playback range that aren't cached yet get
simulated quietly and go into the same cache playback and renders use.

It can't get a core of its own: Blender only evaluates a scene from its main thread,
and a Python thread would fight the interface for Python's lock anyway. So it works in
short slices on the main thread, rests at least twice as long as it worked (a third of
one core at most), and stops at the first sign of activity.

The frames are evaluated in a hidden scene (".EmilsWiggle Cache", names starting with a
dot don't show up in the scene list). It holds copies of the rigs without our constraint
that share their armature and action, plus the colliders and winds. Blender only
evaluates what those need, so the heavy meshes are left alone, and that scene's
depsgraph is never made active, so nothing it evaluates leaks back into the real
objects. Results use the same keys as playback, so they're exactly what playing the
range from the start would have cached.

Things that bit here, in Blender 3.6:
- ViewLayer.update() and context.evaluated_depsgraph_get() make a depsgraph active,
  and an active depsgraph copies its results back onto the original objects. Only
  scene.frame_set() (once, it creates the depsgraph) and Depsgraph.update() are used.
- frame_set() redraws every window, and any update of any depsgraph tells every 3D
  view's draw engines that the view changed (that restarts viewport anti-aliasing).
  So after creating the depsgraph we wait until the viewport stops redrawing, and we
  never run while a viewport is in Rendered shading (Cycles would restart).
- Python references to Blender data that got freed by undo or a file load can crash.
  Nothing is kept across ticks except names and pointers that get checked again.
- Never create or delete anything while a render or export has the interface locked.
"""

import math
import re
import time
import traceback

import bpy

from . import runtime, solver

CACHE_SCENE = ".EmilsWiggle Cache"
SCENE_TAG = "emils_wiggle_cache_scene"
COPY_TAG = "emils_wiggle_cache_copy"
DEFAULT_DELAY = 15.0
SLICE = 0.008  # seconds of work per tick
REST = 2.0  # rest at least this many times as long as a slice took
CHUNK = 24  # bone updates between time checks
WARMUP_QUIET = 0.6  # the viewport has to be quiet this long after the cache scene got its depsgraph
WARMUP_MAX = 6.0
SLOW_EVAL = 0.2  # evaluating the copies slower than this, three times, and the scene gets skipped (a hitch if you come back mid-way)
POLL = 0.5

force_enabled = None  # tests: True/False overrides the preference
delay_override = None  # tests: seconds

last_activity = time.monotonic()
last_error = ""
stats = {"sessions": 0, "frames": 0, "interrupted": 0, "draws_ignored": 0, "woken_by": ""}
_last_draw = 0.0
_ignore_draws = False
_session = None
_draw_handles = []


# ---------------------------------------------------------------- activity

_scene_state = {}


def _scene_signature(scene):
    r = scene.render
    return (scene.frame_start, scene.frame_end, scene.use_preview_range, scene.frame_preview_start,
            scene.frame_preview_end, r.fps, r.fps_base, scene.use_gravity, tuple(scene.gravity))


def note_depsgraph(scene, depsgraph):
    """An edit, unless it's only Blender refreshing the scene itself.

    Adding or removing our copies in the hidden scene makes Blender re-sync the real
    scene too, which shows up as an update of just the Scene. That's not the user.
    """
    if SCENE_TAG in scene:
        return
    ids = [u.id for u in depsgraph.updates]
    sig = _scene_signature(scene)
    key = scene.as_pointer()
    if all(isinstance(i, bpy.types.Scene) for i in ids) and _scene_state.get(key) == sig:
        return  # (also when nothing at all changed)
    _scene_state[key] = sig
    note_activity(scene, "edit", depsgraph)


def note_activity(scene=None, source="?", depsgraph=None):
    """Something happened: frame change, edit, undo, render... Background work waits."""
    global last_activity
    if scene is not None:
        try:
            if SCENE_TAG in scene:
                return  # that's us
        except ReferenceError:
            return
    last_activity = time.monotonic()
    if _session is not None:
        stats["woken_by"] = source
        if depsgraph is not None:
            stats["woken_by"] += " " + ", ".join(sorted({u.id.name for u in depsgraph.updates}))[:200]


def _on_draw():
    global last_activity, _last_draw
    now = time.monotonic()
    _last_draw = now
    if _ignore_draws:
        stats["draws_ignored"] += 1
    else:
        last_activity = now
        if _session is not None:
            stats["woken_by"] = "redraw"


def _settings():
    """(enabled, delay in seconds)"""
    enabled, delay = True, DEFAULT_DELAY
    addon = bpy.context.preferences.addons.get(__package__)
    prefs = addon.preferences if addon is not None else None
    if prefs is not None:
        enabled = prefs.background_cache
        delay = float(prefs.background_delay)
    if force_enabled is not None:
        enabled = force_enabled
    if delay_override is not None:
        delay = delay_override
    return enabled, delay


def _locked():
    wm = bpy.context.window_manager
    return bpy.app.is_job_running("RENDER") or (wm is not None and wm.is_interface_locked)


def _busy():
    """Playing, editing a mesh, a Rendered viewport... anything where we'd get in the way."""
    wm = bpy.context.window_manager
    for win in wm.windows:
        screen = win.screen
        if screen is None:
            continue
        if screen.is_animation_playing:
            return True
        for area in screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                if space is not None and space.shading.type == "RENDERED":
                    return True
        view_layer = win.view_layer
        ob = view_layer.objects.active if view_layer is not None else None
        if ob is not None and ob.mode not in {"OBJECT", "POSE"}:
            return True
    return False


# ---------------------------------------------------------------- the hidden scene

def _is_cache_scene(scene):
    return SCENE_TAG in scene


def _shown_scenes():
    wm = bpy.context.window_manager
    return {win.scene.as_pointer() for win in wm.windows if win.scene is not None}


def cache_scene():
    for sc in bpy.data.scenes:
        if SCENE_TAG in sc:
            return sc
    return None


def _scene_by_ptr(ptr):
    for sc in bpy.data.scenes:
        if sc.as_pointer() == ptr:
            return sc
    return None


def _object(name, ptr):
    ob = bpy.data.objects.get(name)
    return ob if ob is not None and ob.as_pointer() == ptr else None


def _ensure_cache_scene(src):
    sc = cache_scene()
    if sc is None:
        sc = bpy.data.scenes.new(CACHE_SCENE)
        sc[SCENE_TAG] = True
    r, sr = sc.render, src.render
    if r.fps != sr.fps or r.fps_base != sr.fps_base:
        r.fps, r.fps_base = sr.fps, sr.fps_base
    if sc.use_gravity != src.use_gravity:
        sc.use_gravity = src.use_gravity
    if tuple(sc.gravity) != tuple(src.gravity):
        sc.gravity = src.gravity
    return sc


def _remove_copies():
    for ob in [o for o in bpy.data.objects if COPY_TAG in o]:
        bpy.data.objects.remove(ob)


def _empty_cache_scene():
    """Take everything back out of the hidden scene (it stays, its depsgraph is worth keeping)."""
    _remove_copies()
    sc = cache_scene()
    if sc is None or sc.as_pointer() in _shown_scenes():
        return
    for ob in list(sc.collection.objects):
        if ob is not None:
            sc.collection.objects.unlink(ob)
    for coll in list(sc.collection.children):
        sc.collection.children.unlink(coll)


def remove_all():
    """Before saving: no hidden scene and no copies in the file."""
    global _session
    if _session is not None:
        stats["interrupted"] += 1
    _session = None
    if _locked():
        return
    _remove_copies()
    shown = _shown_scenes()
    for sc in [s for s in bpy.data.scenes if SCENE_TAG in s]:
        if sc.as_pointer() not in shown and len(bpy.data.scenes) > 1:
            bpy.data.scenes.remove(sc)


def repair_after_undo():
    """Undo can leave freed copies listed in the hidden scene (see runtime.has_holes). Its depsgraph
    would crash copying it, so the scene goes, the next session makes a new one."""
    sc = cache_scene()
    if sc is None or _locked():
        return
    if runtime.has_holes(sc.collection) or any(c is None for c in sc.collection.children):
        if sc.as_pointer() not in _shown_scenes() and len(bpy.data.scenes) > 1:
            bpy.data.scenes.remove(sc)
            print("Emil's Wiggle: removed the background cache scene, undo left a freed copy in it")


def drop():
    """Forget the session without touching any data (undo, file load: it may be gone already)."""
    global _session
    if _session is not None:
        stats["interrupted"] += 1
    _session = None


def _remap(c, ob, copy):
    for attr in ("target", "pole_target", "space_object"):
        if getattr(c, attr, None) == ob:
            setattr(c, attr, copy)
    if c.type == "ARMATURE":
        for t in c.targets:
            if t.target == ob:
                t.target = copy


def _make_copy(ob, sc):
    """The rig without our constraint, sharing its armature and action, in the hidden scene."""
    copy = ob.copy()
    copy.name = ".EmilsWiggle " + ob.name
    copy[COPY_TAG] = ob.name
    sc.collection.objects.link(copy)
    for pb in copy.pose.bones:
        c = runtime.find_constraint(pb)
        if c is not None:
            pb.constraints.remove(c)
        for c in pb.constraints:
            _remap(c, ob, copy)
    for c in copy.constraints:
        _remap(c, ob, copy)
    ad = copy.animation_data
    if ad is not None:
        for fc in ad.drivers:
            for var in fc.driver.variables:
                for t in var.targets:
                    if t.id == ob:
                        t.id = copy
    return copy


# ---------------------------------------------------------------- what the copies may depend on

def _affected_bones(ob, rig):
    """Bones that carry wiggle in the real rig: the wiggle bones and everything below them."""
    names = {b.name for b in rig.bones}
    out = set()
    for pb in ob.pose.bones:
        x = pb
        while x is not None:
            if x.name in names:
                out.add(pb.name)
                break
            x = x.parent
    return out


def _constraint_refs(c):
    refs = []
    for attr, sub in (("target", "subtarget"), ("pole_target", "pole_subtarget"),
                      ("space_object", "space_subtarget")):
        t = getattr(c, attr, None)
        if isinstance(t, bpy.types.Object):
            refs.append((t, getattr(c, sub, "") or ""))
    if c.type == "ARMATURE":
        refs += [(t.target, t.subtarget) for t in c.targets if t.target is not None]
    return refs


_BONE_PATH = re.compile(r'pose\.bones\["([^"]+)"\]')


def _driver_refs(idd):
    ad = getattr(idd, "animation_data", None) if idd is not None else None
    refs = []
    if ad is not None:
        for fc in ad.drivers:
            for var in fc.driver.variables:
                for t in var.targets:
                    if isinstance(t.id, bpy.types.Scene):
                        refs.append((None, "scene"))  # the hidden scene is at another frame than that one
                        continue
                    if not isinstance(t.id, bpy.types.Object):
                        continue
                    if var.type == "SINGLE_PROP":
                        path = t.data_path or ""
                        found = _BONE_PATH.search(path)
                        sub = found.group(1) if found else ("*" if "pose" in path else "")
                    else:
                        sub = t.bone_target or ""
                    refs.append((t.id, sub))
    return refs


def _deformed_by_wiggle(x, rig_ob, envelopes, wiggling):
    """x gets deformed by rig_ob (modifier or armature parent): do wiggling bones move its vertices?"""
    info = wiggling.get(rig_ob.name)
    if info is None or info[0] != rig_ob.as_pointer():
        return False
    return envelopes or bool({vg.name for vg in x.vertex_groups} & info[1])


def _follows_wiggle(x, wiggling, seen, self_ptr=None, transform_only=False):
    """True when x's evaluated state could depend on some rig's wiggle (on the conservative side).

    wiggling: rig name -> (object pointer, bones that carry wiggle)
    self_ptr: the rig being copied, its references to itself point at the copy instead
    transform_only: only x's object transform matters (it's used as a plain object target)
    """
    ptr = x.as_pointer()
    if (ptr, transform_only) in seen:
        return False
    seen.add((ptr, transform_only))
    refs = []  # (object, bone or "" for the object itself or "*" for anything in it)
    if x.parent is not None:
        if x.parent_type in {"BONE", "BONE_RELATIVE"}:
            refs.append((x.parent, x.parent_bone or "*"))
        else:
            refs.append((x.parent, ""))
            if x.parent_type == "ARMATURE" and not transform_only                     and _deformed_by_wiggle(x, x.parent, False, wiggling):
                return True
    for c in x.constraints:
        refs += _constraint_refs(c)
    refs += _driver_refs(x)
    if not transform_only:
        if x.pose is not None:
            for pb in x.pose.bones:
                for c in pb.constraints:
                    if c.name != runtime.CONSTRAINT_NAME:
                        refs += _constraint_refs(c)
        for m in x.modifiers:
            if m.type == "NODES":
                return True  # can't tell what a node tree reads
            if m.type == "ARMATURE" and m.object is not None:
                if m.object.as_pointer() != self_ptr and _deformed_by_wiggle(
                        x, m.object, m.use_bone_envelopes or not m.use_vertex_groups, wiggling):
                    return True
                refs.append((m.object, "#"))  # the rest of its pose still matters
                continue
            for prop in m.bl_rna.properties:
                if prop.type == "POINTER" and getattr(prop.fixed_type, "identifier", "") == "Object":
                    t = getattr(m, prop.identifier)
                    if t is not None:
                        refs.append((t, getattr(m, "subtarget", "") or ""))
        refs += _driver_refs(x.data)
        refs += _driver_refs(getattr(x.data, "shape_keys", None))
    for t, sub in refs:
        if t is None:
            return True  # reads the real scene
        tp = t.as_pointer()
        if tp == self_ptr:
            continue  # the copy points at itself instead
        info = wiggling.get(t.name)
        if info is not None and info[0] == tp:
            if sub == "*" and info[1]:
                return True  # anything in a wiggling rig, can't tell which bone
            if sub not in ("", "*", "#") and sub in info[1]:
                return True
        # its bones matter when a bone or the whole pose is used, else only where it is
        if _follows_wiggle(t, wiggling, seen, None, transform_only=(sub == "")):
            return True
    return False


# ---------------------------------------------------------------- a session

def _mdiff(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(4) for j in range(4))


class _Job:
    def __init__(self, rig):
        self.name = rig.name
        self.ptr = rig.ptr
        self.rig = rig
        self.gen = rig.cache_gen
        self.copy_name = None
        self.copy_ptr = None
        self.bg = None
        self.copy_eval = None
        self.plain_bones = ()  # bones without any wiggle on them, the copy has to match those exactly
        self.first = None  # first frame to simulate
        self.restart = None  # cached frame to go on from, None to start from rest
        self.active = True


def _give_up(rig, reason):
    rig.bg_done = rig.cache_gen
    rig.bg_reason = reason


class _Session:
    def __init__(self, scene, view_layer, rt, rigs, start, end, key):
        self.scene_ptr = scene.as_pointer()
        self.view_layer = view_layer.name
        self.rt = rt
        self.start, self.end, self.key = start, end, key
        self.jobs = [_Job(r) for r in rigs]
        self.state = "build"
        self.built_at = 0.0
        self.cache_ptr = None
        self.dg_ptr = None
        self.gen = None
        self.slow = 0
        self.done = 0
        self.todo = 0

    # -- checks done at every tick, before touching anything we hold on to
    def resolve(self):
        scene = _scene_by_ptr(self.scene_ptr)
        if scene is None or runtime.peek(scene) is not self.rt or not scene.emils_wiggle.enabled:
            return None, None
        if self.cache_ptr is None:
            return scene, None
        sc = _scene_by_ptr(self.cache_ptr)
        if sc is None:
            return None, None
        dg = sc.view_layers[0].depsgraph if len(sc.view_layers) else None
        if self.dg_ptr is not None and (dg is None or dg.as_pointer() != self.dg_ptr):
            return None, None
        for job in self.jobs:
            if job.active and job.copy_ptr is not None and _object(job.copy_name, job.copy_ptr) is None:
                return None, None
        return scene, sc

    def build(self, scene):
        rt = self.rt
        wiggling = {}
        for rig in rt.rigs.values():
            ob = scene.objects.get(rig.name)
            if ob is not None and ob.as_pointer() == rig.ptr:
                wiggling[rig.name] = (rig.ptr, _affected_bones(ob, rig))
        view_layer = scene.view_layers.get(self.view_layer)
        real_dg = view_layer.depsgraph if view_layer is not None else None
        sc = _ensure_cache_scene(scene)
        _empty_cache_scene()
        in_scene = set()
        for job in self.jobs:
            rig = job.rig
            ob = scene.objects.get(job.name)
            if ob is None or ob.as_pointer() != job.ptr or not rig.ready or rig.last_frame is None:
                job.active = False  # playback hasn't set it up yet, try another time
                continue
            if rig.anim_dirty:
                runtime.detect_settings_animated(rig, ob)
            if rig.settings_dirty:
                # any edit marks the settings for a re-read, which normally waits for the next frame
                ob_eval = ob.evaluated_get(real_dg) if real_dg is not None else ob
                if ob_eval.as_pointer() == ob.as_pointer():
                    job.active = False
                    continue
                try:
                    runtime.refresh_settings(scene, rig, ob, ob_eval)
                except Exception:
                    job.active = False  # something changed under it, playback sorts it out
                    continue
            reason = self._blocker(ob, rig, scene, wiggling)
            if reason:
                _give_up(rig, reason)
                job.active = False
                continue
            job.first, job.restart = self._resume_point(rig)
            if job.first is None:
                rig.bg_done = rig.cache_gen
                job.active = False
                continue
            copy = _make_copy(ob, sc)
            job.copy_name, job.copy_ptr = copy.name, copy.as_pointer()
            affected = wiggling[job.name][1] if job.name in wiggling else set()
            job.plain_bones = tuple(pb.name for pb in ob.pose.bones if pb.name not in affected)
            bg = runtime._build_rig(ob, rig.signature)
            for b in bg.bones:
                real = rig.by_name[b.name]
                b.tail, b.head, b.has_pin_constraint = real.tail, real.head, real.has_pin_constraint
            bg.colliders, bg.winds = set(rig.colliders), set(rig.winds)
            bg.settings_animated = rig.settings_animated
            bg.settings_dirty = False
            job.bg = bg
            self.todo += self.end - job.first + 1
            for name in rig.colliders | rig.winds:
                x = scene.objects.get(name)
                if x is not None and x.as_pointer() not in in_scene:
                    sc.collection.objects.link(x)
                    in_scene.add(x.as_pointer())
            if rig.settings_animated:
                for coll in _collider_collections(ob, scene):
                    if coll.name not in sc.collection.children:
                        sc.collection.children.link(coll)
        self.cache_ptr = sc.as_pointer()
        if not any(job.active for job in self.jobs):
            return False
        dg = sc.view_layers[0].depsgraph
        warm = dg is None
        if warm:
            sc.frame_set(self.start)  # creates the depsgraph (not active), redraws every window once
            dg = sc.view_layers[0].depsgraph
        self.dg_ptr = dg.as_pointer()
        self.built_at = time.monotonic()
        self.state = "warmup"  # adding the copies redraws things too, let that pass
        return True

    def _blocker(self, ob, rig, scene, wiggling):
        if _follows_wiggle(ob, wiggling, set(), self_ptr=ob.as_pointer()):
            return "it depends on another wiggling rig or on scene settings"
        for name in sorted(rig.colliders | rig.winds):
            x = scene.objects.get(name)
            if x is not None and _follows_wiggle(x, wiggling, set()):
                return f"'{name}' moves with wiggling bones or scene settings"
        return ""

    def _resume_point(self, rig):
        f = self.start
        while f <= self.end:
            entry = rig.cache.get(f)
            if entry is None or entry[0] != self.key or entry[2]:
                break
            f += 1
        if f > self.end:
            return None, None
        return f, (None if f == self.start else f - 1)

    def run(self, scene, sc):
        """Generator doing the actual work, a yield between every little piece."""
        rt = self.rt
        s = scene.emils_wiggle
        dg = sc.view_layers[0].depsgraph
        jobs = [j for j in self.jobs if j.active]

        # The copies have to move exactly like the real rigs. Checked on the current frame against the
        # real rig as Blender evaluated it: the object and every bone that carries no wiggle.
        view_layer = scene.view_layers.get(self.view_layer)
        real_dg = view_layer.depsgraph if view_layer is not None else None
        sc.frame_current = scene.frame_current
        dg.update()
        for job in jobs:
            ob = scene.objects.get(job.name)
            copy = _object(job.copy_name, job.copy_ptr)
            real_eval = ob.evaluated_get(real_dg) if (real_dg is not None and ob is not None) else None
            if real_eval is None or real_eval.as_pointer() == ob.as_pointer():
                job.active = False
                continue
            copy_eval = copy.evaluated_get(dg)
            size = max(1.0, real_eval.matrix_world.translation.length)
            worst = _mdiff(real_eval.matrix_world, copy_eval.matrix_world)
            real_bones, copy_bones = real_eval.pose.bones, copy_eval.pose.bones
            for name in job.plain_bones:
                worst = max(worst, _mdiff(real_bones[name].matrix, copy_bones[name].matrix))
            if worst / size > 1e-4:
                _give_up(job.rig, "its copy doesn't move exactly like it (drivers or constraints on other objects?)")
                job.active = False
            yield

        frame = min((j.first for j in jobs if j.active), default=None)
        while frame is not None and frame <= self.end:
            live = [j for j in jobs if j.active and j.first <= frame]
            if not any(j.active for j in jobs):
                return
            if live:
                sc.frame_current = frame
                t0 = time.perf_counter()
                dg.update()
                if time.perf_counter() - t0 > SLOW_EVAL:
                    self.slow += 1
                    if self.slow >= 3:
                        for job in jobs:
                            _give_up(job.rig, "evaluating it takes too long to do quietly")
                        return
                yield
                for job in live:
                    copy = _object(job.copy_name, job.copy_ptr)
                    job.copy_eval = copy.evaluated_get(dg)
                    if job.bg.settings_animated:
                        runtime.refresh_settings(sc, job.bg, copy, job.copy_eval)
                world = runtime._build_world(sc, s, dg, [j.bg for j in live])
                for job in live:
                    yield from self._frame(job, frame, s, world)
                    if not job.active:
                        continue
                    if not self._store(job, frame, scene):
                        return
                    self.done += 1
                    stats["frames"] += 1
            frame += 1
            yield
        for job in jobs:
            if job.active and job.rig.cache_gen == job.gen:
                job.rig.bg_done = job.gen
                job.rig.bg_reason = ""

    def _frame(self, job, frame, s, world):
        """Exactly what _step_rig does for a RESET or a one frame SIM."""
        bg = job.bg
        bones = bg.bones
        starting = frame == job.first
        if starting and job.restart is not None:
            entry = job.rig.cache.get(job.restart)
            if entry is None or entry[0] != self.key:
                job.active = False
                return
            for b in bones:
                snap = entry[1].get(b.name)
                if snap is not None:
                    solver.restore(b, snap)
        fresh = starting and job.restart is None
        runtime._read_inputs(bg, job.copy_eval, shift=not fresh, fast=False)
        if fresh:
            solver.prepare_view(bones, 1.0)
            solver.reset(bones)
            if s.preroll:
                yield from solver.settle_iter(bones, world, s.preroll * s.substeps, CHUNK)
        else:
            yield from solver.simulate_iter(bones, world, s.substeps, CHUNK)
        if not runtime._all_finite(bg):
            _give_up(job.rig, "the simulation blows up, playback deals with that")
            job.active = False

    def _store(self, job, frame, scene):
        rig = self.rt.rigs.get(job.name)
        if rig is not job.rig or rig.cache_gen != job.gen:
            job.active = False  # the rig changed, its results are worthless now
            return True
        if scene.emils_wiggle.cache_locked:
            return False
        old = rig.cache.get(frame)
        if old is not None and old[0] == self.key and not old[2]:
            return True
        snaps = {b.name: solver.snapshot(b) for b in job.bg.bones}
        if not runtime.store_background(self.rt, rig, frame, self.key, snaps):
            for j in self.jobs:
                _give_up(j.rig, "the cache is full")
            return False
        return True


def _collider_collections(ob, scene):
    out = []
    for pb in ob.pose.bones:
        s = pb.emils_wiggle
        for side, used in ((s.tail, s.use_tail), (s.head, s.use_head)):
            coll = side.collider_collection
            if used and side.collider_type == "Collection" and coll is not None \
                    and coll in scene.collection.children_recursive and coll not in out:
                out.append(coll)
    return out


# ---------------------------------------------------------------- picking work

def _needs_work(rig, start, end, key):
    if rig.bg_done == rig.cache_gen:
        return False
    cache = rig.cache
    for f in range(start, end + 1):
        entry = cache.get(f)
        if entry is None or entry[0] != key or entry[2]:
            return True
    rig.bg_done = rig.cache_gen
    rig.bg_reason = ""
    return False


def _pick():
    wm = bpy.context.window_manager
    for win in wm.windows:
        scene = win.scene
        if scene is None or _is_cache_scene(scene):
            continue
        s = scene.emils_wiggle
        if not (s.enabled and s.use_cache) or s.cache_locked:
            continue
        rt = runtime.peek(scene)
        if rt is None or rt.rendering:
            continue
        if rt.needs_edit or rt.structure_dirty or runtime._stale(scene, rt):
            # an edit marked the rigs for a rebuild, which normally waits for the next frame
            runtime._edit_now(scene, rt)
            if rt.needs_edit or rt.structure_dirty:
                continue
        if not rt.rigs:
            continue
        start, end = runtime.playback_range(scene)
        if end < start:
            continue
        bones = sum(len(r.bones) for r in rt.rigs.values())
        end = min(end, start + int(runtime.CACHE_BUDGET * 0.8 / max(1, bones)))
        if runtime.cache_total(rt) >= runtime.CACHE_BUDGET * 0.8:
            continue
        key = runtime.reset_key(start, s)
        rigs = [r for r in rt.rigs.values() if _needs_work(r, start, end, key)]
        if rigs and win.view_layer is not None:
            return _Session(scene, win.view_layer, rt, rigs, start, end, key)
    return None


def _tag_sidebars():
    for win in bpy.context.window_manager.windows:
        if win.screen is None:
            continue
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()


def _end(interrupted):
    global _session, _ignore_draws
    if _session is not None and interrupted:
        stats["interrupted"] += 1
    _session = None
    _ignore_draws = False
    _empty_cache_scene()
    _tag_sidebars()


# ---------------------------------------------------------------- the timer

def _tick():
    global last_error
    try:
        return _step()
    except Exception:
        last_error = traceback.format_exc()
        print("Emil's Wiggle: background cache failed")
        traceback.print_exc()
        if _session is not None:
            for job in _session.jobs:
                _give_up(job.rig, "it failed, see the debug report")
        try:
            if not _locked():
                _end(True)
        except Exception:
            drop()
        return 2.0


def _step():
    global _session, _ignore_draws
    if bpy.app.background:
        return None
    enabled, delay = _settings()
    if _locked():
        return POLL  # a render or an export is running: don't touch any data, not even to clean up
    if not enabled:
        if _session is not None or any(COPY_TAG in o for o in bpy.data.objects):
            _end(True)
        return 1.0
    if _busy():
        note_activity(source="busy")
    now = time.monotonic()
    if now - last_activity < delay:
        if _session is not None:
            _end(True)
        elif any(COPY_TAG in o for o in bpy.data.objects):
            _empty_cache_scene()  # left over from undo or an autosave
        return POLL

    if _session is None:
        _session = _pick()
        if _session is None:
            return 1.0
        stats["sessions"] += 1
        return 0.02

    scene, sc = _session.resolve()
    if scene is None:
        _end(True)
        return 1.0
    session = _session
    if session.state == "build":
        _ignore_draws = True  # our own changes redraw the windows once
        if not session.build(scene):
            _end(False)
            return 1.0
        return 0.1
    if session.state == "warmup":
        if now - session.built_at > WARMUP_MAX:
            _end(True)  # the viewport keeps redrawing, maybe it's still sampling
            note_activity()
            return POLL
        if now - max(_last_draw, session.built_at) < WARMUP_QUIET:
            return 0.1
        session.state = "run"
    if session.gen is None:
        _ignore_draws = False
        session.gen = session.run(scene, sc)
        _tag_sidebars()

    t0 = time.perf_counter()
    try:
        while True:
            next(session.gen)
            if time.perf_counter() - t0 >= SLICE:
                break
    except StopIteration:
        _end(False)
        return 1.0
    spent = time.perf_counter() - t0
    if session.done % 10 == 0:
        _tag_sidebars()
    return max(0.02, spent * REST)


def run_blocking(scene, limit=None):
    """Tests: one session for `scene`, right now, ignoring idle time and redraws.

    limit: stop after that many little steps, like when the user comes back.
    """
    global _session
    rt = runtime.peek(scene)
    if rt is None or rt.structure_dirty or runtime._stale(scene, rt):
        return 0
    s = scene.emils_wiggle
    start, end = runtime.playback_range(scene)
    key = runtime.reset_key(start, s)
    rigs = [r for r in rt.rigs.values() if _needs_work(r, start, end, key)]
    if not rigs:
        return 0
    session = _Session(scene, bpy.context.view_layer, rt, rigs, start, end, key)
    _session = session
    try:
        if session.build(scene):
            scene, sc = session.resolve()
            for steps, _ in enumerate(session.run(scene, sc), 1):
                if limit is not None and steps >= limit:
                    break
    finally:
        _end(limit is not None)
    return session.done


def status(scene):
    """One line for the panel, or ''."""
    rt = runtime.peek(scene)
    if rt is None:
        return ""
    if _session is not None and _session.rt is rt:
        if _session.state != "run":
            return "Background cache: getting ready"
        return f"Background cache: {_session.done}/{max(_session.todo, 1)} frames"
    reasons = sorted({f"{r.name}: {r.bg_reason}" for r in rt.rigs.values()
                      if r.bg_reason and r.bg_done == r.cache_gen})
    return ("Background cache skipped " + "; ".join(reasons)) if reasons else ""


# ---------------------------------------------------------------- register

_SPACES = ("SpaceView3D", "SpaceProperties", "SpaceOutliner", "SpaceGraphEditor", "SpaceDopeSheetEditor",
           "SpaceNLA", "SpaceNodeEditor", "SpaceImageEditor", "SpaceSequenceEditor", "SpaceTextEditor",
           "SpaceClipEditor", "SpaceConsole", "SpaceInfo", "SpacePreferences", "SpaceFileBrowser",
           "SpaceSpreadsheet")


def register():
    global last_activity
    last_activity = time.monotonic()
    if bpy.app.background:
        return
    for name in _SPACES:
        space = getattr(bpy.types, name, None)
        if space is None:
            continue
        try:
            _draw_handles.append((space, space.draw_handler_add(_on_draw, (), "WINDOW", "POST_PIXEL")))
        except Exception:
            pass
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=2.0, persistent=True)


def unregister():
    # Also runs when Blender quits: forget everything, delete nothing (see __init__.py).
    drop()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for space, handle in _draw_handles:
        try:
            space.draw_handler_remove(handle, "WINDOW")
        except Exception:
            pass
    _draw_handles.clear()
