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
same code runs during F12 / Ctrl+F12 renders on the render thread. Nothing gets
created or deleted from that thread (or any other one: Alembic/USD exports and
compositor scenes call the frame handlers from their own threads too).

frame_change_pre never creates or deletes anything either, it only picks up the
empties that already exist. When Blender switches a window to another scene it
builds that scene's depsgraph, runs frame_change_pre, then builds it again without
evaluating in between, and 3.6 crashes in that second build if the handler added
objects or constraints. Setting up empties/constraints happens in frame_change_post
(after the evaluation) or in a timer on the main thread instead, see request_edits().
"""

import itertools
import math
import threading
import time
import traceback
from array import array
from collections import Counter, deque

import bpy
from mathutils import Matrix, Vector

from . import solver

CONSTRAINT_NAME = "Emil's Wiggle"
HELPER_TAG = "emils_wiggle_helper"
HELPER_COLLECTION = "EmilsWiggle Helpers"
MAX_SKIP = 4  # frames simulated in one go when scrubbing forward; more is treated as a jump
MAX_STRETCH = 2.0  # longest step (in frames) when playback drops more frames than that
CACHE_BUDGET = 250_000  # cached bone-frames per scene (about 1.4 KB each), far frames go first
PIN_TYPES = {"DAMPED_TRACK", "TRACK_TO", "LOCKED_TRACK"}
IDENTITY = Matrix.Identity(4)
DELTA = 13  # index of the wiggle offset in a solver snapshot

CACHE, SIM, RESET, SAME, SUB = "CACHE", "SIM", "RESET", "SAME", "SUB"

_runtimes = {}
_MAIN_THREAD = threading.main_thread()
history = deque(maxlen=150)  # (frame, rig, what happened), for the debug report
_edit_keys = itertools.count(1)
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
        self.collider_colls = set()  # collider collections the colliders came from
        self.winds = set()
        self.cache = {}  # frame -> (key, {bone: snapshot}, approximate)
        self.cache_gen = 0  # goes up whenever the cache gets cleared
        self.approx = False  # the state came from skipped frames, don't let it replace exact ones
        self.approx_since = None  # frame where the guessing started, None when unknown
        self.why = ""  # why _decide picked what it picked, for the debug report
        self.bg_done = None  # cache_gen the background cache finished (or gave up) on
        self.bg_reason = ""
        self.fast_reason = ""
        self.blowups = 0  # times the sim blew up and started over, for the panel
        self.blowup_frame = None
        self.blowup_bones = []
        self.pose_state = None  # pose mode only: what a click could change, see _only_clicked
        self.rest_state = None
        self.pick_state = None
        self.constraint_state = None
        self.pending = None
        self.pre_applied = set()


class SceneRuntime:
    def __init__(self):
        self.rigs = {}
        self.structure_dirty = True
        self.object_count = -1
        self.own_writes = set()  # rigs whose empties we moved outside a frame change
        self.key_counter = 0
        self.skip_static_preroll = False
        self.force_fast = False
        self.rendering = False
        self.needs_edit = False  # a read-only rebuild left empties/constraints to set up
        self.scene_sig = None  # fps and gravity the cache was made with
        self.collider_state = {}  # view layer -> which colliders are visible / in collider collections
        self.legacy_found = False
        self.stats_sim_ms = 0.0
        self.frame_ms = 0.0  # playback: time between frames
        self.last_play_frame = None
        self.last_play_time = 0.0
        self.hitch = ""  # playback: this frame came late, for the debug report
        self.frame_moved = False  # a frame change or playback start since the last depsgraph update
        self.counts = Counter()

    def new_key(self, tag, base=None):
        """A key for a new run. Guessed runs ("skip") carry the exact run they were guessed from."""
        self.key_counter += 1
        return (tag, self.key_counter, base) if tag == "skip" else (tag, self.key_counter)


def exact_run(key):
    """The exact run a key's frames follow: the key itself, or for guessed frames the run they came from."""
    if key is not None and key[0] == "skip":
        return key[2]
    return key


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


def _on_main_thread():
    return threading.current_thread() is _MAIN_THREAD


def _can_edit_data(rt):
    # Renders from the UI run on their own thread. Background renders are on the main one.
    return _on_main_thread() and (not rt.rendering or bpy.app.background)


# ---------------------------------------------------------------- helper empties

def _editable(ob):
    if ob.library is None:
        return True
    override = ob.override_library
    return override is not None and not getattr(override, "is_system_override", False)


def _owner_key(ob, pb):
    return f"{ob.name}/{pb.name}"


def _is_helper(ob):
    return ob is not None and HELPER_TAG in ob


def find_constraint(pb):
    c = pb.constraints.get(CONSTRAINT_NAME)
    if c is not None and c.type == "COPY_TRANSFORMS":
        return c
    for c in pb.constraints:
        if c.type == "COPY_TRANSFORMS" and _is_helper(c.target):
            return c
    return None


def helper_collection(create=True):
    """All empties live in one collection that isn't linked to any scene."""
    coll = bpy.data.collections.get(HELPER_COLLECTION)
    if (coll is None or coll.library is not None) and create:
        coll = bpy.data.collections.new(HELPER_COLLECTION)
        coll.use_fake_user = True
    return coll


def has_holes(coll):
    """A collection that lists an object Blender already freed. Undo leaves those behind when an
    object made since the last undo step goes away but the collection is kept as it was in
    memory (our empties get made by timers and handlers, never by an operator). Blender crashes
    as soon as anything walks such a collection in C (copying it, foreach_get/set...)."""
    return any(o is None for o in coll.objects)


_needs_repair = False


def repair_helper_collection():
    """Rebuild our collection without the holes. Main thread only, never during a render."""
    global _needs_repair
    _needs_repair = False
    coll = bpy.data.collections.get(HELPER_COLLECTION)
    if coll is None or coll.library is not None or not has_holes(coll):
        return False
    keep = [o for o in coll.objects if o is not None]
    bpy.data.collections.remove(coll)  # frees the list without looking at what's in it
    fresh = helper_collection(create=True)
    for o in keep:
        fresh.objects.link(o)
    print(f"Emil's Wiggle: rebuilt the {HELPER_COLLECTION} collection, undo left a freed empty in it")
    for rt in _runtimes.values():
        rt.structure_dirty = True
    return True


def ensure_helper(ob, pb, taken):
    """Make sure the bone has our constraint and its own empty. Returns the empty."""
    c = find_constraint(pb)
    helper = c.target if c is not None else None
    key = _owner_key(ob, pb)
    if helper is not None and (not _is_helper(helper) or helper.get(HELPER_TAG) != key
                               or helper.as_pointer() in taken):
        # not ours, or it belongs to another bone (duplicated bone/rig, scene copy, rename)
        helper = None
    if helper is None:
        helper = bpy.data.objects.new(f"EmilsWiggle_{ob.name}_{pb.name}", None)
        helper.empty_display_size = 0.05
        helper[HELPER_TAG] = key
    if helper.rotation_mode != "QUATERNION":
        helper.rotation_mode = "QUATERNION"
    coll = helper_collection()
    if coll not in helper.users_collection:
        # (not by name: an empty linked from a library can share its name with a local object)
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


def existing_helper(ob, pb, taken):
    """Read-only version of ensure_helper: the bone's empty if it already has its own, else None."""
    c = find_constraint(pb)
    helper = c.target if c is not None else None
    if (helper is None or not _is_helper(helper) or helper.get(HELPER_TAG) != _owner_key(ob, pb)
            or helper.as_pointer() in taken):
        return None  # a copied rig still points at the original's empty, never move that one
    taken.add(helper.as_pointer())
    return helper


def remove_constraint(pb):
    """Take our constraint off a bone. The empty goes away in cleanup_orphan_helpers()."""
    c = find_constraint(pb)
    if c is not None:
        pb.constraints.remove(c)
        return True
    return False


def strip_object(ob):
    if ob.type != "ARMATURE" or ob.pose is None or not _editable(ob):
        return False
    removed = False
    for pb in ob.pose.bones:
        removed |= remove_constraint(pb)
    return removed


def cleanup_orphan_helpers():
    """Delete empties that no constraint in the whole file points at anymore."""
    used = set()
    for ob in bpy.data.objects:
        if ob.type == "ARMATURE" and ob.pose is not None:
            for pb in ob.pose.bones:
                for c in pb.constraints:
                    if c.type == "COPY_TRANSFORMS" and c.target is not None:
                        used.add(c.target.as_pointer())
    orphans = [ob for ob in bpy.data.objects if _is_helper(ob) and ob.as_pointer() not in used]
    for ob in orphans:
        bpy.data.objects.remove(ob)
    if orphans:
        # a runtime somewhere might still hold one of them
        for rt in _runtimes.values():
            rt.structure_dirty = True
    return len(orphans)


def strip_scene(scene):
    for ob in scene.objects:
        strip_object(ob)
    cleanup_orphan_helpers()


def neutralize_helpers():
    """Put every empty back to identity without deleting anything (safe while Blender quits)."""
    coll = helper_collection(create=False)
    if coll is None or not len(coll.objects) or has_holes(coll):
        return
    objects = coll.objects
    n = len(objects)
    objects.foreach_set("location", [0.0] * (3 * n))
    objects.foreach_set("rotation_quaternion", [1.0, 0.0, 0.0, 0.0] * n)
    objects.foreach_set("scale", [1.0] * (3 * n))
    for ob in objects:
        ob.update_tag(refresh={"OBJECT"})


bad_writes = 0  # empties that would have gotten a non-finite transform (debug report)


class HelperWriter:
    """Moves the empties in one go.

    A normal RNA write also pushes a UI notifier, and that queue isn't thread safe,
    which matters on the render thread. foreach_set + update_tag only tags the depsgraph.
    Empties that aren't in the helper collection anymore are skipped, never touched.
    """

    def __init__(self):
        self.jobs = []

    def show(self, b, m):
        if b.helper is None or (b.shown is not None and _close(b.shown, m)):
            return
        self.jobs.append((b, m))
        b.shown = m.copy()

    def flush(self):
        global bad_writes, _needs_repair
        if not self.jobs:
            return 0
        coll = helper_collection(create=False)
        members = list(coll.objects) if coll is not None else []
        if any(o is None for o in members):
            # foreach_get/set would crash, and this can run anywhere (render thread, frame_change_pre)
            self.jobs.clear()
            _needs_repair = True
            request_edits()
            return 0
        objects = coll.objects if coll is not None else ()
        n = len(members)
        index = {o.as_pointer(): i for i, o in enumerate(members)}
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
            values = l[:] + r[:] + sc[:]
            if not all(math.isfinite(x) and abs(x) < MAX_POSITION for x in values):
                l, r, sc = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0)  # never hand Blender inf/nan
                bad_writes += 1
            loc[3 * i:3 * i + 3] = l[:]
            rot[4 * i:4 * i + 4] = r[:]
            sca[3 * i:3 * i + 3] = sc[:]
            batched.append(objects[i])
        if batched:
            objects.foreach_set("location", loc)
            objects.foreach_set("rotation_quaternion", rot)
            objects.foreach_set("scale", sca)
            for helper in batched:
                helper.update_tag(refresh={"OBJECT"})
        self.jobs.clear()
        return len(batched)


# ---------------------------------------------------------------- structure

def _rig_signature(ob):
    """Everything a rig's structure depends on. A change means the rig gets rebuilt.

    None while the pose isn't linked to its bones (right after an undo, until Blender rebuilds it).
    """
    if any(pb.bone is None for pb in ob.pose.bones):
        return None
    entries = []
    for pb in ob.pose.bones:
        s = pb.emils_wiggle
        if s.mute:
            continue
        bone = pb.bone
        has_tail = s.use_tail
        has_head = s.use_head and not bone.use_connect
        if has_tail or has_head:
            entries.append((pb.name, has_tail, has_head))
    if not entries:
        return ()
    # hierarchy and inheritance of every bone, wiggle chains depend on the in-between ones too
    shape = tuple((pb.name, pb.parent.name if pb.parent else "", pb.bone.use_connect,
                   pb.bone.inherit_scale, pb.bone.use_inherit_rotation, round(pb.bone.length, 6))
                  for pb in ob.pose.bones)
    return tuple(entries), shape


def _depth(pb):
    d = 0
    p = pb.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _build_rig(ob, signature):
    rig = RigRuntime(ob, signature)
    active = {name: (tail, head) for name, tail, head in signature[0]}
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


def _attach(ob, rig, taken, edit):
    """Give the rig's bones their empties. Returns True when an empty may have lost its bone."""
    dropped = False
    for pb in ob.pose.bones:
        b = rig.by_name.get(pb.name)
        if b is None:
            if edit:
                dropped |= remove_constraint(pb)
            continue
        if edit:
            c = find_constraint(pb)
            old = _ptr(c.target) if c is not None else None
            helper = ensure_helper(ob, pb, taken)
            dropped |= old is not None and old != _ptr(helper)  # renamed/copied bone got a new one
        else:
            helper = existing_helper(ob, pb, taken)
        if _ptr(b.helper) != _ptr(helper):
            b.helper = helper
            b.shown = None
    return dropped


def rebuild(scene, rt, edit=True):
    """Match the runtime to the scene. With edit=False nothing is created or deleted,
    and the edits it would have made are left for later (rt.needs_edit)."""
    old_rigs = rt.rigs
    new_rigs = {}
    taken = set()
    removed_something = False
    waiting = False
    enabled = scene.emils_wiggle.enabled
    for ob in scene.objects:
        if ob.type != "ARMATURE" or ob.pose is None:
            continue
        try:
            os_ = ob.emils_wiggle
            signature = _rig_signature(ob) if enabled and not (os_.mute or os_.freeze) else ()
            if signature is None:
                # keep what we had and look again after Blender's next update
                waiting = True
                if ob.name in old_rigs:
                    new_rigs[ob.name] = old_rigs[ob.name]
                continue
            if not signature or not _editable(ob):
                if edit and signature == ():
                    removed_something |= strip_object(ob)
                continue
            rig = old_rigs.get(ob.name)
            if rig is None or rig.ptr != ob.as_pointer() or rig.signature != signature:
                if edit and rig is not None:
                    removed_something = True  # bones may have left the rig
                rig = _build_rig(ob, signature)
            removed_something |= _attach(ob, rig, taken, edit)
            rig.settings_dirty = True  # colliders, winds, constraints may have changed too
            new_rigs[ob.name] = rig
        except Exception:
            # one broken armature (odd override, locked data...) shouldn't stop the others
            global last_error
            last_error = traceback.format_exc()
            rt.counts["errors"] += 1
            print(f"Emil's Wiggle: couldn't set up {ob.name}")
            traceback.print_exc()
    if edit and (removed_something or any(name not in new_rigs for name in old_rigs)):
        cleanup_orphan_helpers()
    rt.rigs = new_rigs
    rt.structure_dirty = waiting
    rt.object_count = len(scene.objects)
    rt.needs_edit = not edit or waiting
    if rt.needs_edit:
        request_edits()


def _edit_now(scene, rt):
    global last_error
    try:
        rebuild(scene, rt, edit=True)
    except Exception:
        rt.needs_edit = False  # don't retry a broken setup every frame
        rt.counts["errors"] += 1
        last_error = traceback.format_exc()
        print("Emil's Wiggle: setting up the rigs failed")
        traceback.print_exc()


def _apply_edits():
    """Timer: do the edits read-only rebuilds left behind, once it's safe."""
    try:
        wm = getattr(bpy.context, "window_manager", None)
        busy = bpy.app.is_job_running("RENDER") or (wm is not None and wm.is_interface_locked)
        again = False
        if _needs_repair:
            if busy:
                again = True
            else:
                repair_helper_collection()
        for scene in bpy.data.scenes:
            rt = peek(scene)
            if rt is None or not rt.needs_edit:
                continue
            if busy:
                again = True
                continue
            rt.rendering = False  # no render job is running
            _edit_now(scene, rt)  # on a disabled scene this takes our constraints off
            if not scene.emils_wiggle.enabled:
                _runtimes.pop(scene.as_pointer(), None)
        return 0.25 if again else None
    except Exception:
        traceback.print_exc()
        return None


def request_edits():
    """Ask for the pending edits to happen on the main thread soon. Only callable from there."""
    if not _on_main_thread() or bpy.app.timers.is_registered(_apply_edits):
        return
    bpy.app.timers.register(_apply_edits, first_interval=0.0)


def _fast_problem(pb):
    bone = pb.bone
    others = [c.name for c in pb.constraints if c.name != CONSTRAINT_NAME]
    if others:
        return f"'{pb.name}' has another constraint ({others[0]})"
    if not bone.use_inherit_rotation:
        return f"'{pb.name}' doesn't inherit rotation"
    if bone.inherit_scale != "FULL":
        return f"'{pb.name}' doesn't inherit scale fully"
    if not bone.use_local_location:
        return f"'{pb.name}' has Local Location off"
    if bone.use_relative_parent:
        return f"'{pb.name}' uses Relative Parenting"
    return ""


def _check_fast(rig, ob):
    reason = ""
    for name, _parent in rig.involved:
        pb = ob.pose.bones.get(name)
        reason = f"'{name}' is missing" if pb is None else _fast_problem(pb)
        if reason:
            break
    rig.fast_ok = not reason
    rig.fast_reason = reason


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


def _orig(idd):
    return idd.original if idd is not None else None


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
    wind = _orig(src.wind_ob)
    sd.wind_ob = wind.name if wind is not None else None
    sd.wind = src.wind
    sd.radius = src.radius
    sd.friction = src.friction
    sd.bounce = src.bounce
    sd.sticky = src.sticky
    sd.chain = src.chain
    colliders = []
    sd.collection = None
    if src.collider_type == "Object":
        c = _orig(src.collider)
        if c is not None and c.type == "MESH" and scene.objects.get(c.name) == c:
            colliders.append(c.name)
    else:
        coll = _orig(src.collider_collection)
        sd.collection = coll.name if coll is not None else None
        if coll is not None and (coll == scene.collection or coll in scene.collection.children_recursive):
            colliders = [o.name for o in coll.all_objects if o.type == "MESH"]
    sd.colliders = colliders
    return sd


def refresh_settings(scene, rig, ob, ob_eval):
    """Read the settings, from the evaluated armature so animated values are right in renders."""
    rig.colliders = set()
    rig.collider_colls = set()
    rig.winds = set()
    bones = ob.pose.bones
    bones_eval = ob_eval.pose.bones
    for b in rig.bones:
        pb = bones.get(b.name)
        pbe = bones_eval.get(b.name)
        if pb is None or pbe is None:
            raise KeyError(f"bone {b.name} is gone")
        s = pbe.emils_wiggle
        b.tail = _read_side(s.tail, scene)
        b.head = _read_side(s.head, scene)
        for side, used in ((b.tail, b.has_tail), (b.head, b.has_head)):
            if used:
                rig.colliders.update(side.colliders)
                if side.collection:
                    rig.collider_colls.add(side.collection)
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
    rig.cache_gen += 1
    rig.converged_key = None
    # The sim goes on from where the bones are, which isn't where the new settings would have
    # put them (they still sag from the gravity that just got turned off, say). Those frames
    # mustn't pass for the run from the first frame, or a later loop replays them in the middle
    # of the real run and the bones twitch.
    rig.key = ("edited", next(_edit_keys))


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


def _store(rig, frame, s, rt=None):
    old = rig.cache.get(frame)
    if old is not None and (s.cache_locked or (rig.approx and not old[2])):
        return  # locked, or an exact frame that a result from skipped frames shouldn't replace
    entry = (rig.key, {b.name: solver.snapshot(b) for b in rig.bones}, rig.approx)
    if not s.use_cache:
        # keep the frame before too, motion blur needs its neighbours
        rig.cache = {f: e for f, e in rig.cache.items() if f == frame - 1}
    new = frame not in rig.cache
    rig.cache[frame] = entry
    if new and rt is not None:
        _trim_cache(rt, rig, frame)


def cache_total(rt):
    """Cached bone-frames in a scene, what CACHE_BUDGET counts."""
    return sum(len(r.cache) * max(1, len(r.bones)) for r in rt.rigs.values())


def store_background(rt, rig, frame, key, snaps):
    """A frame the background cache simulated. Returns False once the cache is nearly full."""
    if cache_total(rt) >= CACHE_BUDGET * 0.85:
        return False
    rig.cache[frame] = (key, snaps, False)
    return True


def reset_key(frame, s):
    """Key of a simulation that starts from rest at `frame` (what playback does)."""
    return ("reset", frame, s.preroll)


def _restore_frame(rig, frame):
    key, snaps, approx = rig.cache[frame]
    for b in rig.bones:
        snap = snaps.get(b.name)
        if snap is not None:
            solver.restore(b, snap)
    rig.key = key
    rig.approx = approx
    rig.approx_since = None  # a guessed frame from the cache, no telling how far off it is


def _trim_cache(rt, rig, frame):
    """A very long timeline shouldn't eat all the memory: drop the frames furthest away."""
    total = cache_total(rt)
    if total <= CACHE_BUDGET:
        return
    per = max(1, len(rig.bones))
    drop = min(len(rig.cache) - 1, -(-(total - int(CACHE_BUDGET * 0.9)) // per))
    if drop <= 0:
        return
    for f in sorted(rig.cache, key=lambda f: abs(f - frame), reverse=True)[:drop]:
        del rig.cache[f]
    rt.counts["cache trimmed"] += drop


# ---------------------------------------------------------------- callbacks from props/ops

def settings_changed(scene, structure=False):
    rt = get(scene)
    for rig in rt.rigs.values():
        rig.settings_dirty = True
        rig.blowups = 0
        rig.blowup_bones = []
        invalidate(rig, scene=scene)
    if structure:
        rt.structure_dirty = True
        if scene.emils_wiggle.enabled:
            rebuild(scene, rt, edit=_can_edit_data(rt))


def scene_enable_changed(scene):
    rt = get(scene)
    edit = _can_edit_data(rt)
    rebuild(scene, rt, edit)
    if not scene.emils_wiggle.enabled and edit:
        _runtimes.pop(scene.as_pointer(), None)  # otherwise the timer strips the bones first


def reset_scene(scene):
    """Forget the simulation and the cache, bones start from rest on the next frame."""
    rt = get(scene)
    rt.rigs = {}
    scene.emils_wiggle.cache_locked = False
    rebuild(scene, rt, edit=_can_edit_data(rt))
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


def _max_skip(scene, playing, rendering):
    """How many frames forward still count as "the next frame" instead of a jump."""
    if rendering:
        return max(MAX_SKIP, scene.frame_step)  # renders with a Frame Step
    if playing:
        # slow playback drops frames, keep simulating through them instead of starting over
        r = scene.render
        return max(MAX_SKIP, int(round(r.fps / r.fps_base)))
    return MAX_SKIP


def _decide(scene, s, rig, frame, playing, rendering=False, continuing=False):
    """(CACHE|RESET|SAME, frame) or (SIM, frame, frames to advance, looped, restart from frame).

    continuing: playback comes straight from the frame this rig was last on, however many
    frames Blender skipped (a slow frame can skip more than a second's worth).
    """
    rig.why = ""
    entry = rig.cache.get(frame)
    if not rig.ready:
        rig.why = "not set up yet"
        return (CACHE, frame) if entry is not None else (RESET, frame)
    last = rig.last_frame
    if frame == last:
        return (CACHE, frame) if entry is not None else (SAME, frame)
    if entry is not None and s.cache_locked:
        return (CACHE, frame)
    most = _max_skip(scene, playing, rendering)
    k = None
    wrap = False
    restart = None
    if last is not None:
        if 0 < frame - last <= most or (continuing and frame > last):
            k = frame - last
        elif playing and frame < last:
            # only real playback loops around, a render or a jump back to the start doesn't
            start, end = playback_range(scene)
            if s.loop:
                e = (end - last) + (frame - start) + 1
                if last <= end and frame >= start and 0 < e <= most:
                    k, wrap = e, True
            elif entry is None and start < frame <= start + most:
                # looped while dropping frames: carry on from the cached first frame
                first = rig.cache.get(start)
                if first is not None and first[0] == reset_key(start, s) and not first[2]:
                    k, restart = frame - start, start
    if k is not None:
        if entry is not None and restart is None:
            if entry[0] == rig.key and (not wrap or rig.key == rig.converged_key):
                return (CACHE, frame)  # replaying the same run, or a loop that settled into a repeat
            # Back onto the exact run after skipping a few frames. Only the run the guesses came
            # from, and only soon after: another run (the background cache starts from the first
            # frame, playback may have started from rest somewhere else) looks like a reset.
            if (rig.approx and not entry[2] and not wrap and entry[0] == exact_run(rig.key)
                    and rig.approx_since is not None and frame - rig.approx_since <= most):
                rig.why = "back on the exact run"
                return (CACHE, frame)
        return (SIM, frame, k, wrap, restart)
    if entry is not None and s.use_cache:
        return (CACHE, frame)
    if last is None:
        rig.why = "first frame"
    elif playing:
        rig.why = f"playback went from frame {last} to {frame}"
    else:
        rig.why = f"jumped from frame {last}"
    return (RESET, frame)


def _subframe_delta(rig, b, frame, t):
    """Wiggle between two frames for motion blur, from the cache (never simulated)."""
    def delta(f):
        entry = rig.cache.get(f)
        snap = entry[1].get(b.name) if entry is not None else None
        return snap[DELTA] if snap is not None else None
    d0, d1 = delta(frame), delta(frame + 1)
    if d0 is not None and d1 is not None:
        return d0.lerp(d1, t)
    dp = delta(frame - 1)
    if d0 is not None and dp is not None:
        return dp.lerp(d0, 1.0 + t)  # next frame isn't known yet, carry on the motion
    return d0 if d0 is not None else b.delta


def _scene_signature(scene):
    """Scene settings the simulation depends on (animated ones are left out, they'd change every frame)."""
    r = scene.render
    ad = scene.animation_data
    animated = set()
    if ad is not None:
        sources = list(ad.drivers) + (list(ad.action.fcurves) if ad.action is not None else [])
        animated = {fc.data_path for fc in sources}
    return (
        None if "render.fps" in animated else r.fps,
        None if "render.fps_base" in animated else r.fps_base,
        scene.use_gravity,
        None if "gravity" in animated else tuple(scene.gravity),
    )


def _collider_state(scene, rt, depsgraph):
    """What collisions depend on: visible colliders/winds and what's in the collider collections."""
    names = set()
    colls = set()
    for rig in rt.rigs.values():
        names |= rig.colliders | rig.winds
        colls |= rig.collider_colls
    visible = set()
    for name in names:
        ob = scene.objects.get(name)
        if ob is not None and ob.evaluated_get(depsgraph).as_pointer() != ob.as_pointer():
            visible.add(name)
    members = []
    inside = None
    for name in sorted(colls):
        coll = bpy.data.collections.get(name)
        if coll is None:
            members.append((name, None))
            continue
        if inside is None:
            inside = {c.as_pointer() for c in scene.collection.children_recursive}
        found = coll.as_pointer() in inside
        members.append((name, found and frozenset(o.name for o in coll.all_objects if o.type == "MESH")))
    return frozenset(visible), tuple(members)


def _colliders_changed(scene, rt, depsgraph):
    """Remember the collider state for this depsgraph's view layer. True if it differs from last time."""
    key = depsgraph.view_layer.name
    state = _collider_state(scene, rt, depsgraph)
    old = rt.collider_state.get(key)
    rt.collider_state[key] = state
    return old is not None and old != state


def _check_scene_settings(scene, rt):
    sig = _scene_signature(scene)
    if sig != rt.scene_sig:
        if rt.scene_sig is not None:
            for rig in rt.rigs.values():
                invalidate(rig, scene=scene)  # another fps or gravity: every cached frame is off
        rt.scene_sig = sig


def _stale(scene, rt):
    """Objects added/removed, or a rig or one of its bones renamed/deleted."""
    if rt.object_count != len(scene.objects):
        return True
    for rig in rt.rigs.values():
        ob = scene.objects.get(rig.name)
        if ob is None or ob.as_pointer() != rig.ptr or ob.pose is None:
            return True
        bones = ob.pose.bones
        if any(bones.get(b.name) is None for b in rig.bones):
            return True
    return False


def _validate(scene, rt, edit):
    if rt.structure_dirty or _stale(scene, rt):
        rebuild(scene, rt, edit)


def _time_playback(rt, frame, scene):
    now = time.perf_counter()
    rt.hitch = ""
    if rt.last_play_frame is not None and now - rt.last_play_time < 2.0:
        ms = (now - rt.last_play_time) * 1000.0
        rt.frame_ms = ms if rt.frame_ms <= 0.0 else rt.frame_ms * 0.8 + ms * 0.2
        jump = frame - rt.last_play_frame
        if jump > 1:
            rt.counts["dropped frames"] += jump - 1
        if ms > 1500.0 * scene.render.fps_base / scene.render.fps:
            rt.hitch = f", came {ms:.0f} ms after frame {rt.last_play_frame}"
    rt.last_play_frame = frame
    rt.last_play_time = now


def frame_pre(scene):
    s = scene.emils_wiggle
    if not s.enabled:
        return
    rt = get(scene)
    rt.own_writes = set()
    _validate(scene, rt, edit=False)  # creating things here can crash Blender, see the top
    if not rt.rigs:
        return
    _check_scene_settings(scene, rt)
    frame = scene.frame_current
    sub = scene.frame_current_final - frame
    rendering = rt.rendering or not _on_main_thread()
    playing = rt.force_fast or (not rendering and _is_playing())
    live_play = playing and not rendering and not rt.force_fast
    if not rendering:
        rt.frame_moved = True
    prev_play = rt.last_play_frame
    if not live_play:
        rt.hitch = ""
    elif frame != rt.last_play_frame:
        _time_playback(rt, frame, scene)
    # Fast Preview only while the viewport plays, never for renders
    fast_allowed = s.fast_preview and playing and not rendering
    writer = HelperWriter()
    for rig in rt.rigs.values():
        if rig.anim_dirty and _can_edit_data(rt):
            ob = scene.objects.get(rig.name)
            if ob is not None:
                detect_settings_animated(rig, ob)
        rig.pre_applied.clear()
        if sub > 1e-4 and rig.ready:
            # motion blur step: show the wiggle in between, leave the simulation alone
            rig.pending = (SUB, frame, sub)
            rig.fast = False
            for b in rig.bones:
                writer.show(b, _subframe_delta(rig, b, frame, sub))
            continue
        continuing = live_play and prev_play is not None and rig.last_frame == prev_play
        decision = _decide(scene, s, rig, frame, playing, rendering, continuing)
        rig.pending = decision
        cached = decision[0] == CACHE
        # Fast Preview shows the wiggle one frame late, cached frames too: showing those on time
        # skipped a frame of wiggle when playback reached cached frames and repeated one when
        # it left them, a little stutter wherever the cache had a gap.
        rig.fast = fast_allowed and rig.fast_ok and (decision[0] == SIM or (cached and rig.ready))
        snaps = rig.cache[frame][1] if cached else None
        for b in rig.bones:
            if rig.fast and b.ready and b.helper is not None:
                # show the previous frame's wiggle now, so there's no second evaluation
                writer.show(b, b.delta)
                b.pre_delta_inv = b.shown.inverted_safe()
                rig.pre_applied.add(b.name)
            elif snaps is not None:
                snap = snaps.get(b.name)
                writer.show(b, snap[DELTA] if snap is not None else IDENTITY)
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
        q = bones.get(b.q_name) if (b.has_head and b.q_name is not None) else None
        if q is None:
            b.qw = None
        elif clean is not None and b.q_name in clean:
            b.qw = clean[b.q_name][0]
        else:
            b.qw = mw @ q.matrix
        b.pin = _read_pin(pbe) if b.has_pin_constraint else None


MAX_POSITION = 1e7  # past this float32 is garbage anyway
MAX_DELTA_SCALE = 1e3  # a wiggle offset is a rotation and a bit of stretch, nowhere near this


def _finite_vec(v):
    return v is not None and all(math.isfinite(x) and abs(x) < MAX_POSITION for x in v)


def _bone_blown(b):
    """Non-finite values, or finite ones so big that Blender would turn them into inf
    (a scale of 1e20 decomposes to an infinite float32 scale)."""
    if not (_finite_vec(b.pos) and _finite_vec(b.vel) and _finite_vec(b.hpos) and _finite_vec(b.hvel)):
        return True
    d = b.delta
    for i in range(3):
        if not (math.isfinite(d[i][3]) and abs(d[i][3]) < MAX_POSITION):
            return True
        for j in range(3):
            if not (math.isfinite(d[i][j]) and abs(d[i][j]) < MAX_DELTA_SCALE):
                return True
    return False


def blown_bones(rig):
    return [b.name for b in rig.bones if _bone_blown(b)]


def _all_finite(rig):
    return not any(_bone_blown(b) for b in rig.bones)


def side_warnings(scene, side, head=False):
    """(icon, title, detail) for settings of one bone end that make no sense or blow the sim up.

    Two short lines each, the sidebar is narrow.

    A tail that can't stretch stays at its length whatever pushes it, so strong forces only
    pull it straight there. A head, or a tail that stretches, can get thrown off for real.
    """
    free = head or side.stretch > 0.0
    out = []
    s = scene.emils_wiggle
    r = scene.render
    dt = r.fps_base / r.fps / max(1, s.substeps)
    if side.per_axis:
        stiff, damp, gravity = max(side.stiff_axis), max(side.damp_axis), max(abs(v) for v in side.gravity_axis)
    else:
        stiff, damp, gravity = side.stiff, side.damp, abs(side.gravity)
    colliding = ((side.collider_type == "Object" and side.collider is not None)
                 or (side.collider_type == "Collection" and side.collider_collection is not None))
    if colliding and side.bounce > 1.0:
        out.append(("ERROR", "Bounce over 1", "adds energy, it'll blow up"))
    if colliding and side.friction > 1.0:
        out.append(("ERROR", "Friction over 1", "overshoots, it'll blow up"))
    if scene.use_gravity and scene.gravity.length * gravity > 1000.0:
        out.append(("ERROR", "Gravity this strong", "can throw the bone off") if free
                   else ("INFO", "Gravity this strong", "just pulls the bone straight"))
    wind = side.wind_ob
    if wind is not None and wind.field is not None and wind.field.type == "WIND" \
            and abs(wind.field.strength * side.wind) / side.mass > 1000.0:
        out.append(("ERROR", "Wind this strong", "for this mass can throw it off") if free
                   else ("INFO", "Wind this strong", "for this mass just pulls it straight"))
    limit = s.iterations / (dt * dt)
    if stiff > limit:
        out.append(("INFO", f"Stiff over {limit:.0f}", "doesn't get any stiffer here"))
    if damp * dt >= 1.0:
        out.append(("INFO", f"Damp of {1.0 / dt:.0f} or more", "stops all motion"))
    return out


def zero_scale(matrix):
    return min(abs(v) for v in matrix.to_scale()) < 1e-6


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
    if rt is None:
        return
    if rt.needs_edit and _can_edit_data(rt):
        _edit_now(scene, rt)  # the pose is evaluated now, safe to add empties and constraints
    if not rt.rigs:
        return
    frame = scene.frame_current
    work = []
    for rig in rt.rigs.values():
        decision = rig.pending
        rig.pending = None
        if decision is None or decision[1] != frame:
            continue
        if decision[0] == SUB:
            rt.counts[SUB] += 1
            history.append((frame, rig.name, f"motion blur step +{decision[2]:.2f}"))
            continue
        ob = scene.objects.get(rig.name)
        if ob is None or ob.as_pointer() != rig.ptr:
            continue
        ob_eval = ob.evaluated_get(depsgraph)
        if ob_eval.as_pointer() == ob.as_pointer():
            continue  # armature isn't part of this depsgraph
        work.append((rig, ob, ob_eval, decision))
    if not work:
        return

    t0 = time.perf_counter()
    world = None
    writer = HelperWriter()
    refreshed = False
    try:
        for rig, ob, ob_eval, decision in work:
            try:
                if rig.settings_dirty or rig.settings_animated:
                    refresh_settings(scene, rig, ob, ob_eval)
                    refreshed = True
                world = _step_rig(scene, s, rt, rig, ob_eval, decision, depsgraph, world, work, writer)
            except Exception:
                # one broken rig shouldn't stop the others, it starts over next frame
                rig.ready = False
                rt.structure_dirty = True
                rt.counts["errors"] += 1
                last_error = traceback.format_exc()
                history.append((decision[1], rig.name, "error"))
                print(f"Emil's Wiggle: simulating {rig.name} failed")
                traceback.print_exc()
    finally:
        writer.flush()
    if depsgraph.mode == "VIEWPORT" and _on_main_thread():
        if refreshed:
            _colliders_changed(scene, rt, depsgraph)  # what later collection updates get compared with
        if not rt.rendering:
            for rig, ob, _ob_eval, _decision in work:
                _remember_pose(rig, ob)  # the frame moved the pose, clicks get compared with this
    rt.stats_sim_ms = (time.perf_counter() - t0) * 1000.0


def _step_rig(scene, s, rt, rig, ob_eval, decision, depsgraph, world, work, writer):
    """Advance one rig to this frame. Returns the (lazily built) World."""
    mode, frame = decision[0], decision[1]
    rt.counts[mode] += 1

    if mode == CACHE:
        _restore_frame(rig, frame)
        rig.last_frame = frame
        rig.ready = all(b.ready for b in rig.bones)
        what = "cache" + (f", {rig.why}" if rig.why else "")
        if rig.fast and depsgraph.mode != "RENDER":
            what += ", fast preview"
        history.append((frame, rig.name, what + rt.hitch))
        return world

    what = mode.lower()
    if mode == RESET and rig.why:
        what += f" ({rig.why})"
    if mode == SIM and decision[4] is not None:
        _restore_frame(rig, decision[4])  # looped while dropping frames, go on from the first frame
        what += f", from frame {decision[4]}"
    fast = rig.fast and mode == SIM
    _read_inputs(rig, ob_eval, shift=(mode == SIM), fast=fast)
    if mode != SAME:
        if world is None:
            world = _build_world(scene, s, depsgraph, [w[0] for w in work])
        if mode == RESET:
            solver.prepare_view(rig.bones, 1.0)
            solver.reset(rig.bones)
            if s.preroll and not rt.skip_static_preroll:
                total = s.preroll * s.substeps
                done = solver.settle(rig.bones, world, total)
                what += f", preroll {done}/{total} steps"
            rig.key = reset_key(frame, s)
            rig.approx = False
            rig.approx_since = None
            rig.converged_key = None
            rig.ready = True
        else:
            k, wrap = decision[2], decision[3]
            # dropped frames: at most MAX_SKIP frames of steps, each one a bit longer
            frames = min(k, MAX_SKIP)
            solver.simulate(rig.bones, world, frames * s.substeps, min(k / frames, MAX_STRETCH))
            if k > 1:
                what += f", {k} frames at once"
            if wrap:  # the timeline looped
                entry = rig.cache.get(frame)
                if k == 1 and entry is not None and _converged(rig, entry[1]):
                    # this loop repeats the previous one, replay it from now on
                    _restore_frame(rig, frame)
                    rig.converged_key = rig.key
                    rt.counts["converged"] += 1
                else:
                    rig.key = rt.new_key("loop")
                    rig.approx = rig.approx or k > 1
            elif k > 1:
                # the frames in between were guessed, keep this run apart from exact ones
                rig.key = rt.new_key("skip", exact_run(rig.key))
                if not rig.approx:
                    rig.approx_since = frame
                rig.approx = True

    bad = blown_bones(rig)
    if bad:
        # extreme settings or a degenerate pose blew the sim up, never hand that to Blender
        rt.counts["unstable"] += 1
        rig.blowups += 1
        rig.blowup_frame = frame
        rig.blowup_bones = bad
        solver.prepare_view(rig.bones, 1.0)
        solver.reset(rig.bones)
        if not _all_finite(rig):
            for b in rig.bones:
                b.delta = IDENTITY.copy()
        rig.key = rt.new_key("reset-unstable")
        rig.approx = True
        history.append((frame, rig.name, "unstable, reset"))

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
    history.append((frame, rig.name, what + rt.hitch))
    rig.last_frame = frame
    _store(rig, frame, s, rt)
    return world


# ---------------------------------------------------------------- clicks in pose mode
# Selecting or hiding a bone tags the armature like a real edit would, and so does any
# click on one of our own properties in the sidebar (Blender tags the owner of every
# add-on property edited from the UI). That used to wipe the cache on every click.
# So while a rig is in pose mode we remember its pose values, rest bones, object matrix
# and selection. An update where only the selection changed, or only a Wiggle Group got
# picked or renamed (note_click), gets ignored. Anything we can't compare (a constraint's
# influence...) never comes together with one of those, so it still clears the cache.

_POSE_VALUES = (("location", 3), ("rotation_quaternion", 4), ("rotation_euler", 3),
                ("rotation_axis_angle", 4), ("scale", 3))
_PICK_FLAGS = ("select", "select_head", "select_tail", "hide")
_clicked = set()  # armatures whose Wiggle Groups got clicked or renamed since the last update


def note_click(ob):
    """One of our sidebar properties on this armature changed, nothing the simulation reads."""
    _clicked.add(ob.as_pointer())


def _floats(collection, prop, size):
    buf = array("f", [0.0]) * (size * len(collection))
    collection.foreach_get(prop, buf)
    return buf.tobytes()


def _pose_state(ob):
    """What changes from frame to frame: pose values and the object matrix."""
    matrix = array("f", [v for row in ob.matrix_world for v in row]).tobytes()
    bones = ob.pose.bones
    return matrix + b"".join(_floats(bones, name, size) for name, size in _POSE_VALUES)


def _pick_state(ob):
    bones = ob.data.bones
    flags = []
    for name in _PICK_FLAGS:
        values = [False] * len(bones)
        bones.foreach_get(name, values)
        flags += values
    active = bones.active
    return active.name if active is not None else None, bytes(flags)


def forget_selection(rig):
    rig.pose_state = rig.rest_state = rig.pick_state = rig.constraint_state = None


def _constraint_state(ob):
    """Bone constraints and rotation modes, minus values that get animated (those can't
    come in the same update as a click anyway)."""
    out = []
    for pb in ob.pose.bones:
        out.append(pb.rotation_mode)
        for c in pb.constraints:
            target = getattr(c, "target", None)
            out.append((pb.name, c.name, c.type, c.mute, c.owner_space, c.target_space,
                        target.as_pointer() if target is not None else 0, getattr(c, "subtarget", "")))
    return out


def _remember_pose(rig, ob, full=False):
    """full: also what only changes with an edit (rest bones, selection, constraints). A frame
    change only needs the pose part, that keeps playback in pose mode cheap."""
    if ob.mode != "POSE" or ob.pose is None:
        forget_selection(rig)
        return
    rig.pose_state = _pose_state(ob)
    if full or rig.rest_state is None:
        rig.rest_state = _floats(ob.data.bones, "matrix_local", 16)
        rig.pick_state = _pick_state(ob)
        rig.constraint_state = _constraint_state(ob)


def _only_clicked(scene, rig, clicked):
    """True when the rig's update was only a click: bones (de)selected or hidden, or a Wiggle
    Group picked or renamed. Remembers the new state either way."""
    ob = scene.objects.get(rig.name)
    if ob is None or ob.as_pointer() != rig.ptr:
        forget_selection(rig)
        return False
    before = (rig.pose_state, rig.rest_state, rig.pick_state, rig.constraint_state)
    _remember_pose(rig, ob, full=True)
    if before[0] is None or rig.pose_state is None:
        return False
    if (rig.pose_state != before[0] or rig.rest_state != before[1] or rig.constraint_state != before[3]
            or _rig_signature(ob) != rig.signature):
        return False
    return clicked or rig.pick_state != before[2]


# ---------------------------------------------------------------- app events

def on_depsgraph_update(scene, depsgraph):
    clicked = set(_clicked)  # only good for the update right after the click
    _clicked.clear()
    if not scene.emils_wiggle.enabled:
        return
    rt = peek(scene)
    if rt is None:
        return
    frame_moved, rt.frame_moved = rt.frame_moved, False
    if bpy.app.is_job_running("RENDER"):
        for rig in rt.rigs.values():
            forget_selection(rig)  # whatever gets clicked meanwhile isn't compared
        return
    if rt.rendering:
        rt.rendering = False  # a render ended without telling us, don't stay stuck
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None and depsgraph.view_layer.name != view_layer.name:
        for rig in rt.rigs.values():
            forget_selection(rig)
        return
    _check_scene_settings(scene, rt)
    if rt.needs_edit or _stale(scene, rt):
        # renamed, added or deleted rigs get set up now, so an F12 right after is right too
        rt.structure_dirty = True
        rt.needs_edit = True
        request_edits()
    if not rt.rigs:
        return
    action_changed = False
    # Collection updates come for all sorts of reasons (deleting any object tags every collection
    # in the file, and the background cache adds and removes objects), so look at what matters.
    # (hiding or excluding a collection only shows up as an update of the scene)
    maybe = depsgraph.id_type_updated("COLLECTION") or any(isinstance(u.id, bpy.types.Scene)
                                                           for u in depsgraph.updates)
    collections_changed = maybe and _colliders_changed(scene, rt, depsgraph)
    touched = set()
    armatures = set()
    # Changing frames from the UI (arrow keys, the timeline, pressing play) sends one more update
    # that lists the scene and everything animated as changed, although nothing was edited. That
    # used to throw the whole cache away every time. It comes right after a frame change, with the
    # scene in it, and never with a rig's armature (a keyframe inserted in object mode has that).
    rig_data = {}
    for rig in rt.rigs.values():
        ob = scene.objects.get(rig.name)
        if ob is not None and ob.data is not None:
            rig_data[rig.name] = ob.data.as_pointer()
    echo = (frame_moved
            and any(isinstance(u.id, bpy.types.Scene) and not u.is_updated_transform
                    and not u.is_updated_geometry for u in depsgraph.updates)
            and not any(isinstance(u.id, bpy.types.Armature) and u.id.original.as_pointer() in rig_data.values()
                        for u in depsgraph.updates))
    if echo:
        rt.counts["frame change echoes"] += 1
    for u in depsgraph.updates:
        idd = u.id
        if echo and isinstance(idd, (bpy.types.Action, bpy.types.Object)):
            continue  # animation moving things, the frame handlers already took care of that
        if isinstance(idd, bpy.types.Action):
            action_changed = True
        elif isinstance(idd, bpy.types.Armature):
            armatures.add(idd.original.as_pointer())  # bones edited, renamed, reparented...
        elif isinstance(idd, bpy.types.Object):
            orig = idd.original
            if not _is_helper(orig) and (u.is_updated_transform or u.is_updated_geometry):
                touched.add(orig.name)
    for name, data in rig_data.items():
        if data in armatures:
            touched.add(name)
    for rig in rt.rigs.values():
        if rig.name in touched and _only_clicked(scene, rig, rig.ptr in clicked):
            touched.discard(rig.name)
            armatures.discard(rig_data.get(rig.name))
            rt.counts["clicks ignored"] += 1
    if rt.own_writes and touched & rt.own_writes and not (armatures or action_changed or collections_changed):
        # just the viewport catching up with empties we moved ourselves (and whatever hangs off those bones)
        rt.own_writes = set()
        return
    if not action_changed and not touched and not collections_changed:
        return
    for rig in rt.rigs.values():
        rig.settings_dirty = True  # renamed or new colliders, winds, constraints...
        if action_changed:
            rig.anim_dirty = True
        if rig.name in touched:
            rt.structure_dirty = True  # bones or our constraints might have changed
        if action_changed:
            why = "an action changed"
        elif collections_changed:
            why = "collections changed"
        elif rig.name in touched:
            why = "the rig changed"
        elif (rig.colliders | rig.winds) & touched:
            why = "a collider or wind moved"
        else:
            continue
        if rig.cache:
            rt.counts["cache cleared: " + why] += 1
        invalidate(rig, scene=scene)


def after_undo():
    repair_helper_collection()
    for scene in bpy.data.scenes:
        rt = peek(scene)
        if rt is None:
            continue
        for rig in rt.rigs.values():
            rig.settings_dirty = True
            rig.anim_dirty = True
            forget_selection(rig)  # undo can bring back a selection and an edit together
            invalidate(rig, scene=scene)
            for b in rig.bones:
                b.helper = None  # the undo step may have swapped the objects
                b.shown = None
        if scene.emils_wiggle.enabled:
            rebuild(scene, rt, edit=False)
        else:
            _runtimes.pop(scene.as_pointer(), None)


def render_started(scene):
    get(scene).rendering = True


def render_finished(scene):
    rt = peek(scene)
    if rt is not None:
        rt.rendering = False
        rt.own_writes = set(rt.rigs)


def playback_started(scene):
    rt = peek(scene)
    if rt is not None:
        rt.frame_moved = True  # pressing play sends the same update a frame change does


def playback_stopped(scene):
    """Fast Preview leaves the last frame one step behind, put the exact wiggle on it."""
    rt = peek(scene)
    if rt is None or not scene.emils_wiggle.enabled:
        return
    rt.last_play_frame = None
    writer = HelperWriter()
    written = set()
    for rig in rt.rigs.values():
        if not rig.fast:
            continue
        rig.fast = False
        for b in rig.bones:
            if b.name in rig.pre_applied:
                writer.show(b, b.delta)
                written.add(rig.name)
        rig.pre_applied.clear()
    if writer.flush():
        rt.own_writes = written


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
