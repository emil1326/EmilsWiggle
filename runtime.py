"""
Emil's Wiggle - runtime: rigs, frame logic, cache, putting the wiggle on the bones.

How the wiggle reaches a bone:
  Every wiggling bone gets a "Copy Transforms" constraint called "Emil's Wiggle"
  (first in the stack, owner Local, target World, mix After Original). It copies a
  hidden empty (kept in a collection that no scene uses). We only ever move those
  empties, never the bone's own loc/rot/scale, so posing and keyframing wiggle
  bones works as usual.

  It also matters for renders: writing pose channels from a frame handler makes
  Blender re-copy the armature for the render, and that copy falls back to stale
  animated values and a stale object matrix (Blender 3.6 has that backup turned
  off). Moving a separate empty never re-copies the armature, so renders are exact.

How a frame goes (see README for the diagram):
  frame_change_pre   empties go to identity (clean pose), or straight to the
                     cached wiggle when the frame is cached
  depsgraph pass 1   Blender evaluates the pose
  frame_change_post  read the evaluated pose, simulate, move the empties
  depsgraph pass 2   Blender evaluates the wiggled pose (skipped for cached frames)

Only the scene/depsgraph given to the handlers are used, never bpy.context, so the
same code runs during F12 / Ctrl+F12 renders on the render thread.
"""

import time
import traceback
from collections import Counter, deque

import bpy
from mathutils import Matrix, Vector

from . import solver

CONSTRAINT_NAME = "Emil's Wiggle"
HELPER_TAG = "emils_wiggle_helper"
HELPER_COLLECTION = "EmilsWiggle Helpers"
MAX_SKIP = 4
PIN_TYPES = {"DAMPED_TRACK", "TRACK_TO", "LOCKED_TRACK"}
IDENTITY = Matrix.Identity(4)

CACHE, SIM, RESET, SAME = "CACHE", "SIM", "RESET", "SAME"

_runtimes = {}
history = deque(maxlen=60)  # (frame, rig, what happened), for the debug report
last_error = ""


class RigRuntime:
    def __init__(self, ob, signature):
        self.name = ob.name
        self.ptr = ob.as_pointer()
        self.signature = signature
        self.bones = []
        self.by_name = {}
        self.involved = []
        self.ready = False
        self.last_frame = None
        self.key = None
        self.converged_key = None
        self.settings_dirty = True
        self.settings_animated = False
        self.anim_dirty = True
        self.fast_ok = False
        self.fast = False
        self.colliders = set()
        self.winds = set()
        self.cache = {}
        self.pending = None
        self.pre_applied = set()


class SceneRuntime:
    def __init__(self):
        self.rigs = {}
        self.structure_dirty = True
        self.object_count = -1
        self.ignore_updates_until = 0.0
        self.key_counter = 0
        self.skip_static_preroll = False
        self.force_fast = False
        self.rendering = False
        self.legacy_found = False
        self.stats_sim_ms = 0.0
        self.counts = Counter()

    def new_key(self, tag):
        self.key_counter += 1
        return (tag, self.key_counter)


def get(scene):
    rt = _runtimes.get(scene.as_pointer())
    if rt is None:
        rt = _runtimes[scene.as_pointer()] = SceneRuntime()
    return rt


def peek(scene):
    return _runtimes.get(scene.as_pointer())


def clear_all():
    _runtimes.clear()


def _ptr(obj):
    """Memory address of a Blender object we hold, or None when Blender already removed it."""
    if obj is None:
        return None
    try:
        return obj.as_pointer()
    except ReferenceError:
        return None


def _close(a, b, eps=1e-6):
    for ra, rb in zip(a, b):
        for x, y in zip(ra, rb):
            if abs(x - y) > eps:
                return False
    return True


# ---------------------------------------------------------------- helper empties

def _editable(ob):
    return ob.library is None or ob.override_library is not None


def find_constraint(pb):
    c = pb.constraints.get(CONSTRAINT_NAME)
    if c is not None and c.type == "COPY_TRANSFORMS":
        return c
    for c in pb.constraints:
        if c.type == "COPY_TRANSFORMS" and c.target is not None and HELPER_TAG in c.target:
            return c
    return None


def helper_collection(create=True):
    """All empties live in one collection that isn't linked to any scene."""
    coll = bpy.data.collections.get(HELPER_COLLECTION)
    if (coll is None or coll.library is not None) and create:
        coll = bpy.data.collections.new(HELPER_COLLECTION)
        coll.use_fake_user = True
    return coll


def ensure_helper(ob, pb, taken):
    """Make sure the bone has our constraint and its own empty. Returns the empty."""
    c = find_constraint(pb)
    helper = c.target if c is not None else None
    if helper is not None and (HELPER_TAG not in helper or helper.as_pointer() in taken):
        helper = None  # not ours, or shared with a duplicated rig
    if helper is None:
        helper = bpy.data.objects.new(f"EmilsWiggle_{ob.name}_{pb.name}", None)
        helper.empty_display_size = 0.05
        helper[HELPER_TAG] = f"{ob.name}/{pb.name}"
    if helper.rotation_mode != "QUATERNION":
        helper.rotation_mode = "QUATERNION"
    coll = helper_collection()
    if coll.objects.get(helper.name) != helper:
        coll.objects.link(helper)
    if c is None:
        c = pb.constraints.new("COPY_TRANSFORMS")
        c.name = CONSTRAINT_NAME
        c.show_expanded = False
    if c.target != helper:
        c.target = helper
    if c.owner_space != "LOCAL":
        c.owner_space = "LOCAL"
    if c.target_space != "WORLD":
        c.target_space = "WORLD"
    if c.mix_mode != "AFTER_FULL":
        c.mix_mode = "AFTER_FULL"
    if c.influence != 1.0:
        c.influence = 1.0
    if c.mute:
        c.mute = False
    index = list(pb.constraints).index(c)
    if index != 0:
        pb.constraints.move(index, 0)
    if helper.parent is not None:
        helper.parent = None
    taken.add(helper.as_pointer())
    return helper


def remove_helper(pb):
    c = find_constraint(pb)
    if c is None:
        return
    helper = c.target
    pb.constraints.remove(c)
    if helper is not None and HELPER_TAG in helper:
        bpy.data.objects.remove(helper)


def strip_object(ob):
    if ob.type != "ARMATURE" or ob.pose is None or not _editable(ob):
        return
    for pb in ob.pose.bones:
        if find_constraint(pb) is not None:
            remove_helper(pb)


def cleanup_orphan_helpers():
    used = set()
    for ob in bpy.data.objects:
        if ob.type == "ARMATURE" and ob.pose is not None:
            for pb in ob.pose.bones:
                for c in pb.constraints:
                    if c.type == "COPY_TRANSFORMS" and c.target is not None:
                        used.add(c.target.as_pointer())
    for ob in list(bpy.data.objects):
        if HELPER_TAG in ob and ob.as_pointer() not in used:
            bpy.data.objects.remove(ob)


def strip_scene(scene):
    for ob in scene.objects:
        strip_object(ob)
    cleanup_orphan_helpers()


def neutralize_helpers():
    """Put every empty back to identity without deleting anything (safe while Blender quits)."""
    coll = helper_collection(create=False)
    if coll is None or not len(coll.objects):
        return
    objects = coll.objects
    n = len(objects)
    objects.foreach_set("location", [0.0] * (3 * n))
    objects.foreach_set("rotation_quaternion", [1.0, 0.0, 0.0, 0.0] * n)
    objects.foreach_set("scale", [1.0] * (3 * n))
    for ob in objects:
        ob.update_tag(refresh={"OBJECT"})


class HelperWriter:
    """Moves the empties in one go.

    A normal RNA write also pushes a UI notifier, and that queue isn't thread safe,
    which matters on the render thread. foreach_set + update_tag only tags the depsgraph.
    """

    def __init__(self):
        self.jobs = []

    def show(self, b, m):
        if b.helper is None or (b.shown is not None and _close(b.shown, m)):
            return
        self.jobs.append((b, m))
        b.shown = m.copy()

    def flush(self):
        if not self.jobs:
            return
        coll = helper_collection(create=False)
        objects = coll.objects if coll is not None else ()
        n = len(objects)
        index = {o.as_pointer(): i for i, o in enumerate(objects)}
        loc, rot, sca = [0.0] * (3 * n), [0.0] * (4 * n), [0.0] * (3 * n)
        if n:
            objects.foreach_get("location", loc)
            objects.foreach_get("rotation_quaternion", rot)
            objects.foreach_get("scale", sca)
        batched = []
        for b, m in self.jobs:
            i = index.get(_ptr(b.helper))
            if i is None:
                # the empty is gone (deleted, undo, new file...), touching it could crash
                b.helper = None
                b.shown = None
                for rt in _runtimes.values():
                    rt.structure_dirty = True
                continue
            l, r, sc = m.decompose()
            loc[3 * i:3 * i + 3] = l[:]
            rot[4 * i:4 * i + 4] = r[:]
            sca[3 * i:3 * i + 3] = sc[:]
            batched.append(b.helper)
        if batched:
            objects.foreach_set("location", loc)
            objects.foreach_set("rotation_quaternion", rot)
            objects.foreach_set("scale", sca)
            for helper in batched:
                helper.update_tag(refresh={"OBJECT"})
        self.jobs.clear()


# ---------------------------------------------------------------- structure

def _rig_signature(ob):
    entries = []
    for pb in ob.pose.bones:
        s = pb.emils_wiggle
        if s.mute:
            continue
        has_tail = s.use_tail
        has_head = s.use_head and not pb.bone.use_connect
        if has_tail or has_head:
            entries.append((pb.name, has_tail, has_head))
    return tuple(entries)


def _depth(pb):
    d = 0
    p = pb.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _build_rig(ob, signature):
    rig = RigRuntime(ob, signature)
    active = {name: (tail, head) for name, tail, head in signature}
    pbs = sorted((ob.pose.bones[name] for name in active), key=_depth)
    for pb in pbs:
        b = solver.BoneState(pb.name)
        b.has_tail, b.has_head = active[pb.name]
        bone = pb.bone
        b.connected = bone.use_connect
        b.full_scale = bone.inherit_scale == "FULL"
        b.length = bone.length
        if pb.parent is not None:
            b.q_name = pb.parent.name
            b.q_length = pb.parent.bone.length
        p = pb.parent  # nearest wiggling ancestor
        while p is not None and p.name not in active:
            p = p.parent
        if p is not None:
            b.parent = rig.by_name[p.name]
            b.parent_is_q = p == pb.parent
        rig.bones.append(b)
        rig.by_name[b.name] = b

    # Bones whose evaluated pose carries a wiggle offset (their own or one from above),
    # on the way down to a wiggle bone. Fast Preview has to take those offsets back out.
    def wiggles_here_or_above(x):
        while x is not None:
            if x.name in active:
                return True
            x = x.parent
        return False

    involved = {}
    for pb in pbs:
        x = pb
        while x is not None and wiggles_here_or_above(x):
            involved[x.name] = x
            x = x.parent
    rig.involved = [(x.name, x.parent.name if x.parent is not None and x.parent.name in involved else None)
                    for x in sorted(involved.values(), key=_depth)]
    return rig


def _attach(ob, rig, taken):
    for pb in ob.pose.bones:
        b = rig.by_name.get(pb.name)
        if b is None:
            if find_constraint(pb) is not None:
                remove_helper(pb)
            continue
        helper = ensure_helper(ob, pb, taken)
        if _ptr(b.helper) != helper.as_pointer():
            b.helper = helper
            b.shown = None


def rebuild(scene, rt):
    old_rigs = rt.rigs
    new_rigs = {}
    taken = set()
    if scene.emils_wiggle.enabled:
        for ob in scene.objects:
            if ob.type != "ARMATURE" or ob.pose is None or not _editable(ob):
                continue
            os_ = ob.emils_wiggle
            signature = () if (os_.mute or os_.freeze) else _rig_signature(ob)
            if not signature:
                strip_object(ob)
                continue
            rig = old_rigs.get(ob.name)
            if rig is None or rig.ptr != ob.as_pointer() or rig.signature != signature:
                rig = _build_rig(ob, signature)
            _attach(ob, rig, taken)
            new_rigs[ob.name] = rig
    else:
        for ob in scene.objects:
            strip_object(ob)
    if any(name not in new_rigs for name in old_rigs) or not scene.emils_wiggle.enabled:
        cleanup_orphan_helpers()
    rt.rigs = new_rigs
    rt.structure_dirty = False
    rt.object_count = len(scene.objects)


def _check_fast(rig, ob):
    ok = True
    for name, _parent in rig.involved:
        pb = ob.pose.bones.get(name)
        if pb is None:
            ok = False
            break
        bone = pb.bone
        others = [c for c in pb.constraints if c.name != CONSTRAINT_NAME]
        if (not bone.use_inherit_rotation or bone.inherit_scale != "FULL" or not bone.use_local_location
                or bone.use_relative_parent or others):
            ok = False
            break
    rig.fast_ok = ok


def detect_settings_animated(rig, ob):
    animated = False
    ad = ob.animation_data
    if ad is not None:
        sources = [ad.drivers]
        if ad.action is not None:
            sources.append(ad.action.fcurves)
        for track in ad.nla_tracks:
            for strip in track.strips:
                if strip.action is not None:
                    sources.append(strip.action.fcurves)
        animated = any("emils_wiggle" in fc.data_path for src in sources for fc in src)
    rig.settings_animated = animated
    rig.anim_dirty = False


def _read_side(src, scene):
    sd = solver.Side()
    sd.mass = src.mass
    sd.stiff = src.stiff
    sd.stretch = src.stretch
    sd.damp = src.damp
    sd.gravity = src.gravity
    sd.per_axis = src.per_axis
    sd.stiff_axis = tuple(src.stiff_axis)
    sd.damp_axis = tuple(src.damp_axis)
    sd.gravity_axis = tuple(src.gravity_axis)
    sd.lock = tuple(src.lock)
    sd.wind_ob = src.wind_ob.name if src.wind_ob is not None else None
    sd.wind = src.wind
    sd.radius = src.radius
    sd.friction = src.friction
    sd.bounce = src.bounce
    sd.sticky = src.sticky
    sd.chain = src.chain
    colliders = []
    if src.collider_type == "Object":
        c = src.collider
        if c is not None and c.type == "MESH" and scene.objects.get(c.name) == c:
            colliders.append(c.name)
    else:
        coll = src.collider_collection
        if coll is not None and (coll == scene.collection or coll in scene.collection.children_recursive):
            colliders = [o.name for o in coll.all_objects if o.type == "MESH"]
    sd.colliders = colliders
    return sd


def refresh_settings(scene, rig, ob):
    rig.colliders = set()
    rig.winds = set()
    for b in rig.bones:
        pb = ob.pose.bones[b.name]
        s = pb.emils_wiggle
        b.tail = _read_side(s.tail, scene)
        b.head = _read_side(s.head, scene)
        for side, used in ((b.tail, b.has_tail), (b.head, b.has_head)):
            if used:
                rig.colliders.update(side.colliders)
                if side.wind_ob:
                    rig.winds.add(side.wind_ob)
        b.has_pin_constraint = b.has_tail and any(c.type in PIN_TYPES for c in pb.constraints)
    _check_fast(rig, ob)
    rig.settings_dirty = False


def _read_pin(pbe):
    for c in pbe.constraints:
        if c.type in PIN_TYPES and c.target is not None and not c.mute:
            t = c.target
            mw = t.matrix_world
            goal = mw.translation
            if c.subtarget and t.type == "ARMATURE" and t.pose is not None:
                sb = t.pose.bones.get(c.subtarget)
                if sb is not None:
                    goal = mw @ sb.head.lerp(sb.tail, c.head_tail)
            return goal.copy(), c.influence
    return None


# ---------------------------------------------------------------- cache

def invalidate(rig, force=False, scene=None):
    if not force and scene is not None and scene.emils_wiggle.cache_locked:
        return
    rig.cache.clear()
    rig.converged_key = None


def invalidate_scene(scene, force=False):
    rt = peek(scene)
    if rt is None:
        return
    for rig in rt.rigs.values():
        invalidate(rig, force, scene)
    if force and scene.emils_wiggle.cache_locked:
        scene.emils_wiggle.cache_locked = False


def cache_info(scene):
    rt = peek(scene)
    frames = set()
    if rt is not None:
        for rig in rt.rigs.values():
            frames.update(rig.cache.keys())
    if not frames:
        return 0, 0, 0
    return len(frames), min(frames), max(frames)


def frame_is_cached(scene, frame):
    rt = peek(scene)
    if rt is None or not rt.rigs:
        return False
    return all(frame in rig.cache for rig in rt.rigs.values())


# ---------------------------------------------------------------- callbacks from props/ops

def settings_changed(scene, structure=False):
    rt = get(scene)
    for rig in rt.rigs.values():
        rig.settings_dirty = True
        invalidate(rig, scene=scene)
    if structure:
        rt.structure_dirty = True
        if scene.emils_wiggle.enabled:
            rebuild(scene, rt)


def scene_enable_changed(scene):
    rt = get(scene)
    rebuild(scene, rt)
    if not scene.emils_wiggle.enabled:
        _runtimes.pop(scene.as_pointer(), None)


def reset_scene(scene):
    """Forget the simulation and the cache, bones start from rest on the next frame."""
    rt = get(scene)
    rt.rigs = {}
    scene.emils_wiggle.cache_locked = False
    rebuild(scene, rt)
    writer = HelperWriter()
    for rig in rt.rigs.values():
        for b in rig.bones:
            writer.show(b, IDENTITY)
    writer.flush()


# ---------------------------------------------------------------- frame logic

def playback_range(scene):
    if scene.use_preview_range:
        return scene.frame_preview_start, scene.frame_preview_end
    return scene.frame_start, scene.frame_end


assume_playing = None  # tests can force this, None means ask the screen


def _is_playing():
    if assume_playing is not None:
        return assume_playing
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return False
    return any(w.screen is not None and w.screen.is_animation_playing for w in wm.windows)


def _decide(scene, s, rig, frame, playing):
    entry = rig.cache.get(frame)
    if not rig.ready:
        return (CACHE, frame) if entry is not None else (RESET, frame)
    last = rig.last_frame
    if frame == last:
        return (CACHE, frame) if entry is not None else (SAME, frame)
    if entry is not None and s.cache_locked:
        return (CACHE, frame)
    k = None
    wrap = False
    if last is not None:
        if 0 < frame - last <= MAX_SKIP:
            k = frame - last
        elif s.loop and playing and frame < last:
            # only real playback loops around, a render or a jump back to the start doesn't
            start, end = playback_range(scene)
            e = (end - last) + (frame - start) + 1
            if last <= end and frame >= start and 0 < e <= MAX_SKIP:
                k, wrap = e, True
    if k is not None:
        # replaying the same run, or a loop that already settled into a repeat
        if entry is not None and entry[0] == rig.key and (not wrap or rig.key == rig.converged_key):
            return (CACHE, frame)
        return (SIM, frame, k, wrap)
    if entry is not None and s.use_cache:
        return (CACHE, frame)
    return (RESET, frame)


def _validate(scene, rt):
    if rt.structure_dirty or rt.object_count != len(scene.objects):
        rebuild(scene, rt)
        return
    for rig in rt.rigs.values():
        ob = scene.objects.get(rig.name)
        if ob is None or ob.as_pointer() != rig.ptr:
            rebuild(scene, rt)
            return


def frame_pre(scene):
    s = scene.emils_wiggle
    if not s.enabled:
        return
    rt = get(scene)
    if not rt.rendering:
        _validate(scene, rt)  # never create data from the render thread
    if not rt.rigs:
        return
    frame = scene.frame_current
    playing = rt.force_fast or (not rt.rendering and _is_playing())
    # Fast Preview only while the viewport plays, never for renders
    fast_allowed = s.fast_preview and playing and not rt.rendering
    writer = HelperWriter()
    for rig in rt.rigs.values():
        if rig.anim_dirty and not rt.rendering:
            ob = scene.objects.get(rig.name)
            if ob is not None:
                detect_settings_animated(rig, ob)
        decision = _decide(scene, s, rig, frame, playing)
        rig.pending = decision
        rig.pre_applied.clear()
        rig.fast = fast_allowed and rig.fast_ok and decision[0] == SIM
        snaps = rig.cache[frame][1] if decision[0] == CACHE else None
        for b in rig.bones:
            if snaps is not None:
                snap = snaps.get(b.name)
                writer.show(b, snap[13] if snap is not None else IDENTITY)
            elif rig.fast and b.ready and b.helper is not None:
                # show the previous frame's wiggle now, so there's no second evaluation
                writer.show(b, b.delta)
                b.pre_delta_inv = b.shown.inverted_safe()
                rig.pre_applied.add(b.name)
            else:
                writer.show(b, IDENTITY)
    writer.flush()


def _build_world(scene, s, depsgraph, rigs):
    scene_eval = depsgraph.scene_eval
    gravity = scene_eval.gravity.copy() if scene_eval.use_gravity else Vector()
    fps = scene.render.fps / scene.render.fps_base
    world = solver.World(1.0 / fps / s.substeps, s.iterations, gravity, depsgraph)
    names = set()
    winds = set()
    for rig in rigs:
        names |= rig.colliders
        winds |= rig.winds
    for name in names:
        ob = scene.objects.get(name)
        if ob is None:
            continue
        ob_eval = ob.evaluated_get(depsgraph)
        if ob_eval.as_pointer() == ob.as_pointer():
            continue  # not evaluated in this depsgraph (hidden / other view layer)
        world.colliders[name] = solver.ColliderInfo(ob, ob_eval.matrix_world.copy())
    for name in winds:
        ob = scene.objects.get(name)
        if ob is None or ob.field is None or ob.field.type != "WIND":
            continue
        world.winds[name] = solver.WindInfo(ob.evaluated_get(depsgraph))
    return world


def _clean_poses(rig, mw, bones):
    """Fast Preview: the evaluated pose already has last frame's wiggle on it, take it back out.

    With full inheritance and no other constraints:
    pose = parent_pose @ rest_offset @ basis @ delta, so
    clean = clean_parent @ parent_pose^-1 @ pose @ delta^-1.
    """
    clean = {}
    for name, parent in rig.involved:
        ev = mw @ bones[name].matrix
        if parent is not None:
            pc, pe_inv = clean[parent][0], clean[parent][1]
            cl = pc @ pe_inv @ ev
        else:
            cl = ev
        if name in rig.pre_applied:
            cl = cl @ rig.by_name[name].pre_delta_inv
        clean[name] = (cl, ev.inverted_safe())
    return clean


def _read_inputs(rig, ob_eval, shift, fast):
    mw = ob_eval.matrix_world
    bones = ob_eval.pose.bones
    clean = _clean_poses(rig, mw, bones) if fast else None
    for b in rig.bones:
        pbe = bones[b.name]
        if shift:
            b.pw_prev = b.pw
            b.qw_prev = b.qw
        else:
            b.pw_prev = b.qw_prev = None
        b.pw = clean[b.name][0] if clean is not None else mw @ pbe.matrix
        if b.has_head and b.q_name is not None:
            if clean is not None and b.q_name in clean:
                b.qw = clean[b.q_name][0]
            else:
                b.qw = mw @ bones[b.q_name].matrix
        else:
            b.qw = None
        b.pin = _read_pin(pbe) if b.has_pin_constraint else None


def _converged(rig, snaps):
    for b in rig.bones:
        snap = snaps.get(b.name)
        if snap is None:
            return False
        tol = 1e-3 * max(b.v_len_world, 1e-6)
        if (b.pos - snap[1]).length > tol or (b.vel - snap[3]).length > tol:
            return False
        if b.has_head and ((b.hpos - snap[4]).length > tol or (b.hvel - snap[6]).length > tol):
            return False
    return True


def frame_post(scene, depsgraph):
    global last_error
    s = scene.emils_wiggle
    if not s.enabled or depsgraph is None:
        return
    rt = peek(scene)
    if rt is None or not rt.rigs:
        return
    frame = scene.frame_current
    work = []
    for rig in rt.rigs.values():
        decision = rig.pending
        rig.pending = None
        if decision is None or decision[1] != frame:
            continue
        ob = scene.objects.get(rig.name)
        if ob is None or ob.as_pointer() != rig.ptr:
            continue
        ob_eval = ob.evaluated_get(depsgraph)
        if ob_eval.as_pointer() == ob.as_pointer():
            continue  # armature isn't part of this depsgraph
        if rig.settings_dirty or rig.settings_animated:
            refresh_settings(scene, rig, ob)
        work.append((rig, ob_eval, decision))
    if not work:
        return

    t0 = time.perf_counter()
    world = None
    writer = HelperWriter()
    try:
        for rig, ob_eval, decision in work:
            try:
                world = _step_rig(scene, s, rt, rig, ob_eval, decision, depsgraph, world, work, writer)
            except Exception:
                # one broken rig shouldn't stop the others, it starts over next frame
                rig.ready = False
                rt.counts["errors"] += 1
                last_error = traceback.format_exc()
                history.append((decision[1], rig.name, "error"))
                print(f"Emil's Wiggle: simulating {rig.name} failed")
                traceback.print_exc()
    finally:
        writer.flush()
    rt.stats_sim_ms = (time.perf_counter() - t0) * 1000.0


def _step_rig(scene, s, rt, rig, ob_eval, decision, depsgraph, world, work, writer):
    """Advance one rig to this frame. Returns the (lazily built) World."""
    mode, frame = decision[0], decision[1]
    rt.counts[mode] += 1

    if mode == CACHE:
        key, snaps = rig.cache[frame]
        for b in rig.bones:
            snap = snaps.get(b.name)
            if snap is not None:
                solver.restore(b, snap)
        rig.key = key
        rig.last_frame = frame
        rig.ready = all(b.ready for b in rig.bones)
        history.append((frame, rig.name, "cache"))
        return world

    fast = rig.fast and mode == SIM
    _read_inputs(rig, ob_eval, shift=(mode == SIM), fast=fast)
    if mode != SAME:
        if world is None:
            world = _build_world(scene, s, depsgraph, [w[0] for w in work])
        if mode == RESET:
            solver.prepare_view(rig.bones, 1.0)
            solver.reset(rig.bones)
            if s.preroll and not rt.skip_static_preroll:
                solver.settle(rig.bones, world, s.preroll * s.substeps)
            rig.key = ("reset", frame, s.preroll)
            rig.converged_key = None
            rig.ready = True
        else:
            solver.simulate(rig.bones, world, decision[2] * s.substeps)
            if decision[3]:  # the timeline looped
                entry = rig.cache.get(frame)
                if entry is not None and _converged(rig, entry[1]):
                    # this loop repeats the previous one, replay it from now on
                    rig.key = rig.converged_key = entry[0]
                    for b in rig.bones:
                        solver.restore(b, entry[1][b.name])
                    rt.counts["converged"] += 1
                else:
                    rig.key = rt.new_key("loop")

    what = mode.lower()
    if mode == SIM and decision[3]:
        what += ", looped" + (" (settled)" if rig.key == rig.converged_key else "")
    if fast and depsgraph.mode != "RENDER":
        rt.counts["fast"] += 1  # the empties keep last frame's wiggle until the next frame
        what += ", fast preview"
    else:
        for b in rig.bones:
            writer.show(b, b.delta)
    if depsgraph.mode == "RENDER":
        what += ", render"
    history.append((frame, rig.name, what))
    rig.last_frame = frame

    if not (s.cache_locked and frame in rig.cache):
        entry = (rig.key, {b.name: solver.snapshot(b) for b in rig.bones})
        if s.use_cache:
            rig.cache[frame] = entry
        else:
            rig.cache = {frame: entry}
    return world


# ---------------------------------------------------------------- app events

def on_depsgraph_update(scene, depsgraph):
    if not scene.emils_wiggle.enabled:
        return
    rt = peek(scene)
    if rt is None or not rt.rigs:
        return
    if bpy.app.is_job_running("RENDER"):
        return
    if rt.rendering:
        rt.rendering = False  # a render ended without telling us, don't stay stuck
        return
    if time.monotonic() < rt.ignore_updates_until:
        return
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None and depsgraph.view_layer.name != view_layer.name:
        return
    action_changed = False
    touched = set()
    for u in depsgraph.updates:
        idd = u.id
        if isinstance(idd, bpy.types.Action):
            action_changed = True
        elif isinstance(idd, bpy.types.Object) and (u.is_updated_transform or u.is_updated_geometry):
            touched.add(idd.original.name)
    if not action_changed and not touched:
        return
    for rig in rt.rigs.values():
        if action_changed:
            rig.anim_dirty = True
        if rig.name in touched:
            rig.settings_dirty = True  # constraints may have changed
            rt.structure_dirty = True  # and ours might have been deleted
        if action_changed or rig.name in touched or (rig.colliders & touched) or (rig.winds & touched):
            invalidate(rig, scene=scene)


def after_undo():
    for scene in bpy.data.scenes:
        rt = peek(scene)
        if rt is None:
            continue
        for rig in rt.rigs.values():
            rig.settings_dirty = True
            rig.anim_dirty = True
            invalidate(rig, scene=scene)
            for b in rig.bones:
                b.helper = None  # the undo step may have swapped the objects
                b.shown = None
        if scene.emils_wiggle.enabled:
            rebuild(scene, rt)
        else:
            _runtimes.pop(scene.as_pointer(), None)


def render_started(scene):
    get(scene).rendering = True


def render_finished(scene):
    rt = peek(scene)
    if rt is not None:
        rt.rendering = False
        rt.ignore_updates_until = time.monotonic() + 1.0


def playback_stopped(scene):
    """Fast Preview leaves the last frame one step behind, put the exact wiggle on it."""
    rt = peek(scene)
    if rt is None or not scene.emils_wiggle.enabled:
        return
    writer = HelperWriter()
    for rig in rt.rigs.values():
        if not rig.fast:
            continue
        rig.fast = False
        for b in rig.bones:
            if b.name in rig.pre_applied:
                writer.show(b, b.delta)
        rig.pre_applied.clear()
    wrote = bool(writer.jobs)
    writer.flush()
    if wrote:
        rt.ignore_updates_until = time.monotonic() + 0.5


def detect_legacy():
    found = False
    for ob in bpy.data.objects:
        if ob.type != "ARMATURE" or ob.pose is None:
            continue
        if any(pb.get("wiggle_tail") or pb.get("wiggle_head") for pb in ob.pose.bones):
            found = True
            break
    for scene in bpy.data.scenes:
        has_ours = any(
            ob.type == "ARMATURE" and ob.pose is not None
            and any(pb.emils_wiggle.use_tail or pb.emils_wiggle.use_head for pb in ob.pose.bones)
            for ob in scene.objects)
        get(scene).legacy_found = found and not has_ours
