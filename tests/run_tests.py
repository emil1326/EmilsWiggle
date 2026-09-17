"""
Headless tests for Emil's Wiggle. Run with Blender 3.6:

    blender -b --factory-startup --python EmilsWiggle/tests/run_tests.py -- <result.txt>

Results (and any traceback) are written to <result.txt>.
"""

import math
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
RESULT = argv[0] if argv else os.path.join(tempfile.gettempdir(), "emils_wiggle_tests.txt")
LOG = RESULT + ".log"
_log = open(LOG, "w", encoding="utf-8", buffering=1)
sys.stdout = _log
sys.stderr = _log
import faulthandler  # noqa: E402
faulthandler.enable(_log)

import bpy  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

import EmilsWiggle  # noqa: E402
from EmilsWiggle import runtime  # noqa: E402

EmilsWiggle.register()

results = []


def check(name, cond, detail=""):
    results.append(("PASS" if cond else "FAIL", name, detail))
    print(("PASS" if cond else "FAIL"), name, detail, flush=True)


def fresh_scene():
    scene = bpy.context.scene
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    s = scene.emils_wiggle
    s.enabled = False
    s.cache_locked = False
    s.loop, s.preroll, s.substeps, s.iterations, s.use_cache = True, 0, 1, 2, True
    if "wiggle_enable" in scene:
        del scene["wiggle_enable"]
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for coll in (bpy.data.armatures, bpy.data.meshes, bpy.data.actions, bpy.data.cameras,
                 bpy.data.images, bpy.data.lights):
        for d in list(coll):
            coll.remove(d)
    for vl in list(scene.view_layers)[1:]:
        scene.view_layers.remove(vl)
    scene.render.engine = "BLENDER_EEVEE"
    scene.frame_start, scene.frame_end = 1, 60
    scene.render.fps, scene.render.fps_base = 30, 1.0
    scene.use_gravity = False
    runtime.clear_all()
    runtime.assume_playing = None
    return scene


def make_chain(name, n=3, scale=1.0, root_motion=((1, 0.0), (8, 2.0), (16, 0.0)), connect=True):
    scene = bpy.context.scene
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    scene.collection.objects.link(ob)
    ob.scale = (scale, scale, scale)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="EDIT")
    root = arm.edit_bones.new("root")
    root.head, root.tail = (0, 0, 0), (0, 0, 1)
    prev = root
    for i in range(n):
        eb = arm.edit_bones.new(f"w{i}")
        eb.head = prev.tail if connect else prev.tail + Vector((0, 0, 0.2))
        eb.tail = eb.head + Vector((0, 0, 1))
        eb.parent = prev
        eb.use_connect = connect
        prev = eb
    bpy.ops.object.mode_set(mode="OBJECT")
    pb = ob.pose.bones["root"]
    for frame, x in root_motion:
        pb.location = (x, 0, 0)
        pb.keyframe_insert("location", frame=frame)
    return ob


def enable(ob, head=False, **side):
    for pb in ob.pose.bones:
        if pb.name.startswith("w"):
            s = pb.emils_wiggle
            s.use_tail = True
            if head:
                s.use_head = True
            for k, v in side.items():
                setattr(s.tail, k, v)


def tail_world(ob, bone="w2"):
    dg = bpy.context.evaluated_depsgraph_get()
    ob_eval = ob.evaluated_get(dg)
    return ob_eval.matrix_world @ ob_eval.pose.bones[bone].tail


def play(scene, ob, frames, bone="w2"):
    out = {}
    for f in frames:
        scene.frame_set(f)
        out[f] = tail_world(ob, bone).copy()
    return out


def max_diff(a, b, scale=1.0):
    return max((a[f] - b[f] * scale).length for f in a)


def helper_mat(ob, bone):
    c = runtime.find_constraint(ob.pose.bones[bone])
    return c.target.matrix_basis.copy() if c is not None and c.target is not None else None


def head_world(ob, bone):
    dg = bpy.context.evaluated_depsgraph_get()
    ob_eval = ob.evaluated_get(dg)
    return ob_eval.matrix_world @ ob_eval.pose.bones[bone].head


def no_leftovers(ob):
    return (all(runtime.find_constraint(pb) is None for pb in ob.pose.bones)
            and not any(runtime.HELPER_TAG in o for o in bpy.data.objects))


def basis_is_identity(pb, eps=1e-5):
    m = pb.matrix_basis
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(4) for j in range(4))


# ------------------------------------------------------------------ tests

def test_basic_and_settle():
    scene = fresh_scene()
    ob = make_chain("Rig")
    frames = range(1, 181)
    rest = play(scene, ob, frames)
    scene.emils_wiggle.enabled = True
    enable(ob)
    wig = play(scene, ob, frames)
    check("wiggle moves the chain", max_diff(wig, rest) > 0.05, f"max diff {max_diff(wig, rest):.4f}")
    settled = (wig[180] - rest[180]).length  # the motion stops on frame 16, default stiffness is soft
    check("chain settles back to rest", settled < 0.005, f"diff at 180: {settled:.5f}")
    check("lock interface turned on", scene.render.use_lock_interface)
    return rest, wig


def test_fps_base():
    trajs = {}
    for fps, base in ((30, 1.0), (3, 0.1), (3, 1.0)):
        scene = fresh_scene()
        scene.render.fps, scene.render.fps_base = fps, base
        ob = make_chain("Rig")
        scene.emils_wiggle.enabled = True
        enable(ob)
        trajs[(fps, base)] = play(scene, ob, range(1, 41))
    d_same = max_diff(trajs[(30, 1.0)], trajs[(3, 0.1)])
    d_other = max_diff(trajs[(30, 1.0)], trajs[(3, 1.0)])
    check("fps 3 / base 0.1 behaves like 30 fps", d_same < 1e-5, f"diff {d_same:.2e}")
    check("fps 3 / base 1 is different (dt matters)", d_other > 1e-3, f"diff {d_other:.4f}")


def test_cache_scrub():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    first = play(scene, ob, range(1, 41))
    count, lo, hi = runtime.cache_info(scene)
    check("playback fills the cache", count == 40 and lo == 1 and hi == 40, f"{count} ({lo}-{hi})")
    scene.frame_set(20)
    d20 = (tail_world(ob) - first[20]).length
    scene.frame_set(21)
    d21 = (tail_world(ob) - first[21]).length
    check("jumping back replays the cached frame", d20 < 1e-5, f"{d20:.2e}")
    check("next frame after a jump matches too", d21 < 1e-5, f"{d21:.2e}")
    t0 = time.perf_counter()
    again = play(scene, ob, range(1, 41))
    dt_replay = time.perf_counter() - t0
    check("replay is identical", max_diff(again, first) < 1e-5, f"{max_diff(again, first):.2e}")
    # continuing past the cache simulates from the cached state
    more = play(scene, ob, range(41, 46))
    runtime.invalidate_scene(scene, force=True)
    runtime.reset_scene(scene)
    straight = play(scene, ob, range(1, 46))
    d = max(( more[f] - straight[f]).length for f in more)
    check("simulating after cached frames == uninterrupted sim", d < 1e-5, f"{d:.2e}")
    return dt_replay


def test_cache_invalidation():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 21))
    bpy.context.evaluated_depsgraph_get()
    count, _, _ = runtime.cache_info(scene)
    check("our own writes don't clear the cache", count == 20, f"{count}")
    pb = ob.pose.bones["root"]
    pb.location = (0, 3, 0)
    pb.keyframe_insert("location", frame=12)
    bpy.context.evaluated_depsgraph_get()
    count, _, _ = runtime.cache_info(scene)
    check("editing the animation clears the cache", count == 0, f"{count}")
    play(scene, ob, range(1, 11))
    scene.emils_wiggle.cache_locked = True
    pb.location = (0, -3, 0)
    pb.keyframe_insert("location", frame=14)
    bpy.context.evaluated_depsgraph_get()
    count, _, _ = runtime.cache_info(scene)
    check("a locked cache survives edits", count == 10, f"{count}")


def test_mute_and_manual_pose():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    w0 = ob.pose.bones["w0"]
    w0.rotation_mode = "XYZ"
    w0.rotation_euler = (0.3, 0.0, 0.0)
    play(scene, ob, range(1, 12))
    e = tuple(w0.rotation_euler)
    check("your own pose channels are never touched",
          abs(e[0] - 0.3) < 1e-7 and abs(e[1]) < 1e-7 and abs(e[2]) < 1e-7, f"{e}")
    check("wiggle bones get the Emil's Wiggle constraint first in the stack",
          all(ob.pose.bones[f"w{i}"].constraints[0].name == runtime.CONSTRAINT_NAME for i in range(3)))
    helper = runtime.find_constraint(w0).target
    coll = runtime.helper_collection(create=False)
    check("helper empties stay out of the scene",
          helper.name not in scene.objects and coll is not None and coll.objects.get(helper.name) == helper
          and coll.name not in scene.collection.children_recursive and coll.use_fake_user)
    check("wiggle offsets the pose while running", not basis_close_identity(helper_mat(ob, "w0")))
    ob.emils_wiggle.mute = True
    check("muting removes the constraints and empties", no_leftovers(ob))
    t0 = time.perf_counter()
    for f in range(1, 31):
        scene.frame_set(f)
    t_muted = time.perf_counter() - t0
    check("a muted armature stays untouched by playback", no_leftovers(ob) and abs(w0.rotation_euler[0] - 0.3) < 1e-7)
    ob.emils_wiggle.mute = False
    play(scene, ob, range(1, 12))
    check("unmuting brings them back", all(runtime.find_constraint(ob.pose.bones[f"w{i}"]) is not None for i in range(3)))
    w1 = ob.pose.bones["w1"]
    w1.emils_wiggle.use_tail = False
    check("unticking a bone removes only its constraint",
          runtime.find_constraint(w1) is None and runtime.find_constraint(w0) is not None)
    w0.constraints.remove(runtime.find_constraint(w0))
    bpy.context.evaluated_depsgraph_get()
    play(scene, ob, range(12, 14))
    check("a deleted constraint comes back on the next frame", runtime.find_constraint(w0) is not None)
    scene.emils_wiggle.enabled = False
    check("turning the scene off cleans everything up", no_leftovers(ob))
    return t_muted


def test_axis_lock_and_per_axis(rest, wig):
    rest = {f: v for f, v in rest.items() if f <= 60}
    wig = {f: v for f, v in wig.items() if f <= 60}
    motion = max_diff(wig, rest)
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob, lock=(True, False, False))
    locked_x = play(scene, ob, range(1, 61))
    d = max_diff(locked_x, rest)
    check("lock X freezes the X swing (root moves on X)", d < 1e-3, f"max diff {d:.5f}")

    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob, lock=(False, False, True))
    locked_z = play(scene, ob, range(1, 61))
    d = max_diff(locked_z, wig)
    check("lock Z leaves the X swing alone", d < 0.01 * motion, f"max diff vs free {d:.5f} (motion {motion:.3f})")

    scene = fresh_scene()
    ob = make_chain("Rig", root_motion=())
    pb = ob.pose.bones["root"]
    for frame, y in ((1, 0.0), (8, 2.0), (16, 0.0)):
        pb.location = (0, 0, y)  # root points up, so its local Z is world -Y
        pb.keyframe_insert("location", frame=frame)
    rest_y = play(scene, ob, range(1, 61))
    scene.emils_wiggle.enabled = True
    enable(ob, lock=(False, False, True))
    locked_zy = play(scene, ob, range(1, 61))
    for b in ob.pose.bones:
        b.emils_wiggle.tail.lock = (False, False, False)
    runtime.reset_scene(scene)
    free_y = play(scene, ob, range(1, 61))
    check("lock Z freezes the swing when moving on Y",
          max_diff(locked_zy, rest_y) < 1e-3 and max_diff(free_y, rest_y) > 0.05,
          f"locked {max_diff(locked_zy, rest_y):.5f}, free {max_diff(free_y, rest_y):.3f}")

    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    for pb in ob.pose.bones:
        if pb.name.startswith("w"):
            pb.emils_wiggle.tail.per_axis = True
    same = play(scene, ob, range(1, 61))
    d = max_diff(same, wig)
    check("per axis with equal values == single values", d < 0.01 * motion, f"{d:.2e}")
    stiff = tuple(ob.pose.bones["w1"].emils_wiggle.tail.stiff_axis)
    check("per axis gets seeded from the single value", stiff == (200.0, 200.0, 200.0), f"{stiff}")

    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    for pb in ob.pose.bones:
        if pb.name.startswith("w"):
            t = pb.emils_wiggle.tail
            t.per_axis = True
            t.stiff_axis = (4000.0, 200.0, 200.0)
    stiffer = play(scene, ob, range(1, 61))
    d_soft = max_diff(wig, rest)
    d_stiff = max_diff(stiffer, rest)
    check("higher X stiffness means less X swing", d_stiff < d_soft * 0.8, f"{d_stiff:.4f} vs {d_soft:.4f}")


def test_scaled_armature():
    scene = fresh_scene()
    ob = make_chain("Big")
    scene.emils_wiggle.enabled = True
    enable(ob)
    big = play(scene, ob, range(1, 41))
    scene = fresh_scene()
    ob = make_chain("Small", scale=0.01)
    scene.emils_wiggle.enabled = True
    enable(ob)
    small = play(scene, ob, range(1, 41))
    d = max((small[f] - big[f] * 0.01).length for f in small) / 0.01
    check("scaled armature wiggles the same (scale fix)", d < 1e-3, f"relative diff {d:.2e}")


def test_collision_pin_head():
    scene = fresh_scene()
    scene.use_gravity = True
    ob = make_chain("Rig", root_motion=((1, 0.0),))
    ob.rotation_euler = (0, 1.5708, 0)  # chain points along +X
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -0.5))
    plane = bpy.context.object
    scene.emils_wiggle.enabled = True
    enable(ob, collider=plane, radius=0.05)
    low = min(p.z for p in play(scene, ob, range(1, 61)).values())
    check("collider stops the chain falling through", low > -0.5, f"lowest z {low:.4f}")

    scene = fresh_scene()
    ob = make_chain("Rig")
    target = bpy.data.objects.new("Target", None)
    scene.collection.objects.link(target)
    target.location = (1.5, 0, 3)
    c = ob.pose.bones["w2"].constraints.new("DAMPED_TRACK")
    c.target = target
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 31))
    d = (tail_world(ob) - target.location).length
    check("damped track pins the tail", d < 0.5, f"distance {d:.3f}")

    scene = fresh_scene()
    ob = make_chain("Rig", connect=False)
    scene.emils_wiggle.enabled = True
    rest_heads = {}
    for f in range(1, 7):
        scene.frame_set(f)
        rest_heads[f] = head_world(ob, "w1").copy()
    enable(ob, head=True)
    moved = 0.0
    for f in range(1, 7):
        scene.frame_set(f)
        moved = max(moved, (head_world(ob, "w1") - rest_heads[f]).length)
    check("head wiggle moves the bone heads", moved > 1e-3, f"max head offset {moved:.4f}")
    traj = play(scene, ob, range(7, 41), bone="w0")
    check("head wiggle stays sane", all(v.length < 100 for v in traj.values()))


def test_render_live_and_cached():
    scene = fresh_scene()
    ob = make_chain("Rig")
    # object level motion and an animated parent, the classic way renders used to break
    ob.keyframe_insert("location", frame=1)
    ob.location.z = 1.5
    ob.keyframe_insert("location", frame=12)
    mover = bpy.data.objects.new("Mover", None)
    scene.collection.objects.link(mover)
    mover.keyframe_insert("rotation_euler", frame=1)
    mover.rotation_euler.z = 0.4
    mover.keyframe_insert("rotation_euler", frame=12)
    ob.parent = mover
    bpy.ops.mesh.primitive_cube_add(size=0.8)
    cube = bpy.context.object
    cube.parent = ob
    cube.parent_type = "BONE"
    cube.parent_bone = "w2"
    scene.view_layers.new("Second")
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (1, -9, 3)
    cam.rotation_euler = (1.5708, 0, 0)
    scene.camera = cam
    scene.frame_end = 12

    scene.emils_wiggle.enabled = True
    enable(ob)
    viewport = {}
    for f in range(1, 13):
        scene.frame_set(f)
        viewport[f] = helper_mat(ob, "w2")

    seen = {}

    def spy(sc, dg):
        if dg is None:
            return
        ob_eval = ob.evaluated_get(dg)
        seen.setdefault(sc.frame_current, []).append(
            (helper_mat(ob, "w2"), ob_eval.as_pointer() != ob.as_pointer(),
             dg.mode))
    bpy.app.handlers.frame_change_post.append(spy)

    tmp = tempfile.mkdtemp()
    try:
        # live: nothing cached, render has to simulate on its own
        runtime.reset_scene(scene)
        scene.frame_set(1)
        runtime.invalidate_scene(scene, force=True)
        runtime.get(scene).rigs["Rig"].ready = False
        scene.render.filepath = os.path.join(tmp, "live_")
        seen.clear()
        bpy.ops.render.render(animation=True)
        live = {f: v for f, v in seen.items()}
        render_frames = sorted(f for f, v in live.items() if any(m == "RENDER" for _b, _e, m in v))
        check("render calls our handlers with the render depsgraph", render_frames[:12] == list(range(1, 13)),
              f"{render_frames}")
        per_layer = [len([1 for _b, _e, m in live[f] if m == "RENDER"]) for f in range(1, 13)]
        check("both view layers are handled", all(n == 2 for n in per_layer), f"{per_layer}")

        def close(a, b):
            return all(abs(a[i][j] - b[i][j]) < 1e-5 for i in range(4) for j in range(4))
        ok = all(close(m, viewport[f]) for f in range(1, 13) for m, _e, mode in live[f] if mode == "RENDER")
        check("live render (no cache) == viewport simulation", ok)
        moving = any(not basis_close_identity(viewport[f]) for f in range(1, 13))
        check("the render actually has wiggle in it", moving)

        # cached: viewport cache then render reads it
        runtime.reset_scene(scene)
        for f in range(1, 13):
            scene.frame_set(f)
        scene.frame_set(5)
        seen.clear()
        scene.render.filepath = os.path.join(tmp, "cached_")
        bpy.ops.render.render(animation=True)
        ok = all(close(m, viewport[f]) for f in range(1, 13) for m, _e, mode in seen[f] if mode == "RENDER")
        check("render from cache == viewport", ok)
        img_live = bpy.data.images.load(os.path.join(tmp, "live_0008.png"))
        img_cached = bpy.data.images.load(os.path.join(tmp, "cached_0008.png"))
        diffs = [abs(a - b) for a, b in zip(img_live.pixels, img_cached.pixels)]
        check("live and cached renders give the same pixels", max(diffs) < 1e-6,
              f"max {max(diffs):.4f}, {sum(1 for d in diffs if d > 0)} values differ")

        # single frame render from the cache (F12 at frame 8)
        scene.frame_set(8)
        scene.render.filepath = os.path.join(tmp, "still.png")
        bpy.ops.render.render(write_still=True)
        img_still = bpy.data.images.load(os.path.join(tmp, "still.png"))
        check("F12 on a cached frame == animation render", list(img_still.pixels) == list(img_cached.pixels))

        # and without wiggle the picture is different
        scene.emils_wiggle.enabled = False
        scene.render.filepath = os.path.join(tmp, "off_")
        scene.frame_start = scene.frame_end = 8
        bpy.ops.render.render(animation=True)
        img_off = bpy.data.images.load(os.path.join(tmp, "off_0008.png"))
        diffs_off = [abs(a - b) for a, b in zip(img_off.pixels, img_cached.pixels)]
        check("render without wiggle differs", max(diffs_off) > 0.1, f"max {max(diffs_off):.3f}")
    finally:
        bpy.app.handlers.frame_change_post.remove(spy)


def basis_close_identity(m, eps=1e-4):
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(4) for j in range(4))


def test_save_load():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 10))
    w2 = ob.pose.bones["w2"]
    check("mid-motion pose has wiggle", not basis_close_identity(helper_mat(ob, "w2")))
    path = os.path.join(tempfile.mkdtemp(), "saved.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    ob = bpy.data.objects["Rig"]
    w2 = ob.pose.bones["w2"]
    check("pose channels in the file are clean", basis_is_identity(w2))
    c = runtime.find_constraint(w2)
    check("constraint and empty survive save/load", c is not None and c.target is not None)
    check("settings survive save/load", w2.emils_wiggle.use_tail and bpy.context.scene.emils_wiggle.enabled)
    scene = bpy.context.scene
    traj = play(scene, ob, range(1, 20))
    check("sim runs after reload", all(v.length < 100 for v in traj.values()))


def test_import_and_copy_and_bake():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene["wiggle_enable"] = True
    for i in range(3):
        pb = ob.pose.bones[f"w{i}"]
        pb["wiggle_tail"] = True
        pb["wiggle_stiff"] = 123.0
        pb["wiggle_collider_type"] = 1
        pb.rotation_quaternion = (0.9, 0.1, 0, 0)
    ob.pose.bones["w1"]["wiggle_damp_head"] = 7.0
    bpy.ops.emils_wiggle.import_wiggle2("EXEC_DEFAULT")
    s = ob.pose.bones["w1"].emils_wiggle
    check("import: tail on", s.use_tail)
    check("import: stiffness", abs(s.tail.stiff - 123.0) < 1e-6, f"{s.tail.stiff}")
    check("import: head damp", abs(s.head.damp - 7.0) < 1e-6)
    check("import: a stiffness Wiggle 2 left at its default stays 400 (ours is 200)",
          abs(s.head.stiff - 400.0) < 1e-6 and ob.pose.bones["root"].emils_wiggle.tail.stiff == 200.0,
          f"{s.head.stiff}")
    check("import: collider type", s.tail.collider_type == "Collection")
    check("import: scene enabled, Wiggle 2 switched off",
          scene.emils_wiggle.enabled and not scene["wiggle_enable"])
    check("import: stale Wiggle 2 pose cleared", basis_is_identity(ob.pose.bones["w1"]))

    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="POSE")
    for pb in ob.pose.bones:
        pb.bone.select = pb.name in ("w0", "w1")
    ob.data.bones.active = ob.data.bones["w0"]
    ob.pose.bones["w0"].emils_wiggle.tail.stiff = 55.0
    check("editing one selected bone edits all selected",
          abs(ob.pose.bones["w1"].emils_wiggle.tail.stiff - 55.0) < 1e-6
          and abs(ob.pose.bones["w2"].emils_wiggle.tail.stiff - 123.0) < 1e-6)
    ob.pose.bones["w0"].emils_wiggle.tail.lock[0] = True
    for pb in ob.pose.bones:
        pb.bone.select = pb.name in ("w0", "w2")
    bpy.ops.emils_wiggle.copy()
    check("copy settings", tuple(ob.pose.bones["w2"].emils_wiggle.tail.lock) == (True, False, False)
          and abs(ob.pose.bones["w2"].emils_wiggle.tail.stiff - 55.0) < 1e-6)

    scene.frame_end = 20
    bpy.ops.emils_wiggle.bake()
    act = ob.animation_data.action
    paths = {fc.data_path for fc in act.fcurves}
    check("bake keys the wiggle bones", any('"w2"' in p for p in paths), f"{sorted(paths)[:4]}")
    check("bake freezes the armature", ob.emils_wiggle.freeze)
    bpy.ops.object.mode_set(mode="OBJECT")


class _MockLayout:
    """Stands in for UILayout so panel draw() code can run in background mode."""
    ICONS = set(bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items.keys())

    def __init__(self, errors):
        object.__setattr__(self, "errors", errors)

    def __setattr__(self, name, value):
        if name not in {"use_property_split", "use_property_decorate", "enabled", "active",
                        "ui_units_x", "alignment", "scale_x", "scale_y"}:
            self.errors.append(f"unknown layout attribute {name}")

    def _icon(self, kw):
        icon = kw.get("icon")
        if icon and icon not in self.ICONS:
            self.errors.append(f"bad icon {icon}")

    def _sub(self, *args, **kw):
        return _MockLayout(self.errors)

    row = column = box = split = column_flow = grid_flow = _sub

    def prop(self, data, name, **kw):
        self._icon(kw)
        if name not in data.bl_rna.properties:
            self.errors.append(f"missing property {name} on {data.bl_rna.identifier}")

    def prop_search(self, data, name, search_data, search_name, **kw):
        self.prop(data, name, **kw)
        if search_name not in search_data.bl_rna.properties:
            self.errors.append(f"missing search collection {search_name}")

    def operator(self, idname, **kw):
        self._icon(kw)
        cat, _, name = idname.partition(".")
        try:
            getattr(getattr(bpy.ops, cat), name).get_rna_type()
        except Exception:
            self.errors.append(f"missing operator {idname}")

    def label(self, **kw):
        self._icon(kw)

    def separator(self, **kw):
        pass

    lists = set()

    def template_list(self, listtype, list_id, data, prop, active_data, active_prop, **kw):
        cls = getattr(bpy.types, listtype, None)
        if cls is None or not issubclass(cls, bpy.types.UIList):
            self.errors.append(f"missing list {listtype}")
            return
        for d, name in ((data, prop), (active_data, active_prop)):
            if name not in d.bl_rna.properties:
                self.errors.append(f"missing property {name} on {d.bl_rna.identifier}")
                return
        for i, item in enumerate(getattr(data, prop)):
            cls.draw_item(None, bpy.context, _MockLayout(self.errors), data, item, 0, active_data, active_prop, i)
        _MockLayout.lists.add(listtype)


def test_ui_draw():
    import types
    from EmilsWiggle import debug, ui
    debug.force_show = True
    scene = fresh_scene()
    ob = make_chain("Rig", connect=False)
    plane = bpy.data.objects.new("Col", bpy.data.meshes.new("Col"))
    scene.collection.objects.link(plane)
    scene.render.use_lock_interface = False
    errors = []
    drawn = set()

    def draw_all(ctx):
        for cls in ui.classes:
            if not issubclass(cls, bpy.types.Panel):
                continue
            if hasattr(cls, "poll") and not cls.poll(ctx):
                continue
            fake = types.SimpleNamespace(layout=_MockLayout(errors))
            if hasattr(cls, "draw_header"):
                cls.draw_header(fake, ctx)
            cls.draw(fake, ctx)
            drawn.add(cls.__name__)

    pb = ob.pose.bones["w1"]
    ctx = types.SimpleNamespace(scene=scene, object=ob, active_pose_bone=pb, mode="POSE",
                                selected_pose_bones=[pb])
    draw_all(ctx)  # scene off
    scene.emils_wiggle.enabled = True
    scene.render.use_lock_interface = False
    runtime.get(scene).legacy_found = True
    draw_all(ctx)  # nothing ticked yet
    s = pb.emils_wiggle
    s.use_tail = s.use_head = True
    draw_all(ctx)  # no groups yet
    from EmilsWiggle import groups
    group = groups.new_group(ob, "Hair")
    s.group = group.uid
    ob.pose.bones["w2"].emils_wiggle.group = 42  # its group is gone
    s.tail.per_axis = True
    s.tail.collider = plane
    s.head.collider_type = "Collection"
    s.head.collider_collection = scene.collection.children[0] if scene.collection.children else None
    play(scene, ob, range(1, 4))
    draw_all(ctx)
    # everything that can warn at once
    rig = runtime.get(scene).rigs["Rig"]
    rig.blowups, rig.blowup_frame, rig.blowup_bones = 3, 2, ["w0", "w1", "w2", "a", "b", "c"]
    s.tail.bounce, s.tail.stiff_axis = 4.0, (1e7, 1e7, 1e7)
    pb.scale = (1.0, 0.0, 1.0)
    draw_all(ctx)
    pb.scale = (1.0, 1.0, 1.0)
    s.tail.bounce = 0.5
    ob.emils_wiggle.mute = True
    draw_all(ctx)
    ob.emils_wiggle.mute = False
    ob.emils_wiggle.freeze = True
    draw_all(ctx)
    ob.emils_wiggle.freeze = False
    scene.emils_wiggle.edit_selected = False
    draw_all(ctx)
    scene.emils_wiggle.edit_selected = True
    draw_all(types.SimpleNamespace(scene=scene, object=None, active_pose_bone=None, mode="OBJECT",
                                   selected_pose_bones=None))
    check("every panel draws without bad names", not errors, "; ".join(sorted(set(errors)))[:300])
    report = debug.build_report(bpy.context)
    print(report)
    check("debug report lists the rig, its bones and recent frames",
          "Rig: 1 bone (1 tail, 1 head)" in report and "w1 (wiggle parent -" in report
          and "tail: mass" in report and "head: mass" in report and "recent frames:" in report)
    check("and the Wiggle Groups", "wiggle groups: Hair #1 (1)" in report and "group Hair #1" in report)
    debug.force_show = False
    drawn |= _MockLayout.lists
    check("all panels got drawn at least once", drawn == {c.__name__ for c in ui.classes},
          f"missing {sorted({c.__name__ for c in ui.classes} - drawn)}")


def test_robustness():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 6))
    w1 = ob.pose.bones["w1"]
    bpy.data.objects.remove(runtime.find_constraint(w1).target)  # someone deletes an empty behind our back
    play(scene, ob, range(6, 10))
    c = runtime.find_constraint(w1)
    check("a deleted empty gets recreated without crashing", c is not None and c.target is not None)

    import EmilsWiggle
    EmilsWiggle.unregister()  # what Reload Scripts and quitting Blender do
    helpers = [runtime.find_constraint(ob.pose.bones[f"w{i}"]) for i in range(3)]
    check("disabling the add-on keeps the setup but takes the wiggle off",
          all(h is not None and basis_close_identity(h.target.matrix_basis) for h in helpers))
    EmilsWiggle.register()
    play(scene, ob, range(1, 9))
    check("enabling it again picks the setup back up",
          scene.emils_wiggle.enabled and not basis_close_identity(helper_mat(ob, "w2")))

    path = os.path.join(tempfile.mkdtemp(), "other.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    check("opening a file forgets the old simulation", all(not rt.rigs for rt in runtime._runtimes.values()))
    scene = bpy.context.scene
    ob = bpy.data.objects["Rig"]
    play(scene, ob, range(1, 6))
    check("and the reopened file simulates fine", runtime.peek(scene) is not None and "Rig" in runtime.peek(scene).rigs)


def _helper_ptrs(ob):
    out = {}
    for pb in ob.pose.bones:
        c = runtime.find_constraint(pb)
        if c is not None and c.target is not None:
            out[pb.name] = c.target.as_pointer()
    return out


def _edit_bones(ob, fn):
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="EDIT")
    fn(ob.data.edit_bones)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.context.evaluated_depsgraph_get()  # the UI evaluates right after an edit


def test_structure_changes():
    scene = fresh_scene()
    check("loop physics is off by default", not bpy.types.Scene.bl_rna.properties["emils_wiggle"]
          .fixed_type.properties["loop"].default)
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 6))
    before = _helper_ptrs(ob)

    # duplicate a wiggle bone in edit mode: the copy keeps the constraint pointing at w1's empty
    def dup(ebs):
        src = ebs["w1"]
        eb = ebs.new("w1_copy")
        eb.head, eb.tail, eb.parent = src.head.copy(), src.tail.copy() + Vector((0.5, 0, 0)), src.parent
    _edit_bones(ob, dup)
    copy = ob.pose.bones["w1_copy"]
    c = copy.constraints.new("COPY_TRANSFORMS")
    c.name = runtime.CONSTRAINT_NAME
    c.target = runtime.find_constraint(ob.pose.bones["w1"]).target  # what Blender's duplicate does
    copy.emils_wiggle.use_tail = True
    play(scene, ob, range(6, 9), bone="w1")
    ptrs = _helper_ptrs(ob)
    check("a duplicated bone gets its own empty", ptrs["w1_copy"] != ptrs["w1"] and ptrs["w1"] == before["w1"])
    copy.emils_wiggle.use_tail = False
    play(scene, ob, range(9, 11), bone="w1")
    c1 = runtime.find_constraint(ob.pose.bones["w1"])
    check("unticking the copy leaves the original's empty alone",
          c1 is not None and c1.target is not None and c1.target.as_pointer() == before["w1"])

    # rename, reparent and delete bones
    ob.pose.bones["w2"].name = "w2_renamed"
    errors = runtime.get(scene).counts.get("errors", 0)
    play(scene, ob, range(11, 14), bone="w1")
    check("a renamed bone keeps wiggling, without errors",
          runtime.find_constraint(ob.pose.bones["w2_renamed"]) is not None
          and "w2_renamed" in runtime.get(scene).rigs["Rig"].by_name
          and runtime.get(scene).counts.get("errors", 0) == errors)
    helpers = sum(1 for o in bpy.data.objects if runtime.HELPER_TAG in o)
    check("no leftover empties after renames and unticks", helpers == 3, f"{helpers} empties")

    def reparent(ebs):
        ebs["w2_renamed"].use_connect = False
        ebs["w2_renamed"].parent = ebs["root"]
    _edit_bones(ob, reparent)
    play(scene, ob, range(14, 17), bone="w1")
    rig = runtime.get(scene).rigs["Rig"]
    check("reparenting a bone rebuilds the chain", rig.by_name["w2_renamed"].parent is None)

    ob.pose.bones["w1"].emils_wiggle.use_head = True
    _edit_bones(ob, lambda ebs: setattr(ebs["w1"], "use_connect", False))
    play(scene, ob, range(17, 20), bone="w1")
    _edit_bones(ob, lambda ebs: ebs.remove(ebs["w0"]))
    errors = runtime.get(scene).counts.get("errors", 0)
    play(scene, ob, range(20, 30), bone="w1")
    check("deleting a bone in the middle of a chain doesn't break anything",
          runtime.get(scene).counts.get("errors", 0) == errors and "w0" not in runtime.get(scene).rigs["Rig"].by_name)


def test_scene_copies():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 6))
    original = _helper_ptrs(ob)
    copy = scene.copy()  # like Scene > New > Full Copy for our purposes
    rig_copy = ob.copy()
    rig_copy.data = ob.data.copy()
    for coll in list(copy.collection.objects):
        copy.collection.objects.unlink(coll)
    copy.collection.objects.link(rig_copy)
    for f in range(1, 6):
        copy.frame_set(f)
    mine = _helper_ptrs(rig_copy)
    check("a copied rig gets its own empties", not (set(mine.values()) & set(original.values())), f"{mine}")
    copy.emils_wiggle.enabled = False
    check("turning wiggle off in the copy keeps the original's empties",
          all(runtime.find_constraint(ob.pose.bones[n]).target is not None for n in original))
    for f in range(6, 12):
        scene.frame_set(f)
    check("and the original keeps wiggling", _helper_ptrs(ob) == original
          and not basis_close_identity(helper_mat(ob, "w2")))
    bpy.data.scenes.remove(copy)


def test_caches_and_updates():
    scene = fresh_scene()
    ob = make_chain("Rig")
    bpy.ops.mesh.primitive_plane_add(size=0.5, location=(9, 9, 9))
    far = bpy.context.object
    coll = bpy.data.collections.new("Colliders")
    scene.collection.children.link(coll)
    scene.emils_wiggle.enabled = True
    enable(ob, collider_type="Collection", collider_collection=coll)
    runtime.assume_playing = True
    play(scene, ob, range(1, 21))
    rt = runtime.get(scene)
    runtime.playback_stopped(scene)
    bpy.context.evaluated_depsgraph_get()  # the viewport catching up with the empties we moved
    count, _, _ = runtime.cache_info(scene)
    check("stopping playback doesn't wipe the cache", count == 20, f"{count}")
    runtime.assume_playing = None
    coll.objects.link(far)
    bpy.context.evaluated_depsgraph_get()
    scene.frame_set(21)
    check("adding a mesh to the collider collection clears the cache and is picked up",
          "Plane" in rt.rigs["Rig"].colliders and runtime.cache_info(scene)[0] <= 2,
          f"{sorted(rt.rigs['Rig'].colliders)} {runtime.cache_info(scene)}")

    # what does and doesn't make the cache stale
    def cached_after(edit):
        play(scene, ob, range(1, 11))
        edit()
        bpy.context.evaluated_depsgraph_get()
        return runtime.cache_info(scene)[0]

    def delete_unrelated():
        junk = bpy.data.objects.new("Junk", None)
        scene.collection.objects.link(junk)
        bpy.context.evaluated_depsgraph_get()
        bpy.data.objects.remove(junk)  # Blender tags every collection when an object goes away
    check("deleting an unrelated object keeps the cache", cached_after(delete_unrelated) >= 10)

    def other_fps():
        scene.render.fps = 24
    check("another frame rate clears the cache", cached_after(other_fps) == 0)
    scene.render.fps = 30

    def more_gravity():
        scene.use_gravity = True
        scene.gravity = (0.0, 0.0, -20.0)
    check("another gravity clears the cache", cached_after(more_gravity) == 0)
    scene.use_gravity = False

    if scene.collection.objects.get(far.name) == far:
        scene.collection.objects.unlink(far)  # only in the collider collection from now on
        bpy.context.evaluated_depsgraph_get()

    def hide_colliders():
        bpy.context.view_layer.layer_collection.children["Colliders"].exclude = True
    # the wiggle settings point at the colliders, so Blender keeps evaluating them and they keep colliding
    left = cached_after(hide_colliders)
    check("excluding the collider collection changes nothing, so the cache stays", left >= 10, f"{left}")
    bpy.context.view_layer.layer_collection.children["Colliders"].exclude = False

    from EmilsWiggle import background
    runtime.invalidate_scene(scene, force=True)
    scene.frame_set(3)
    background.run_blocking(scene)
    bpy.context.evaluated_depsgraph_get()  # Blender's reaction to the copies coming and going
    count = runtime.cache_info(scene)[0]
    check("the background cache coming and going doesn't clear what it made", count == 60, f"{count}")
    background.remove_all()

    from EmilsWiggle import legacy
    Scene = bpy.types.Scene

    class WiggleScene(bpy.types.PropertyGroup):
        lastframe: bpy.props.IntProperty()

    bpy.utils.register_class(WiggleScene)
    Scene.wiggle = bpy.props.PointerProperty(type=WiggleScene)
    scene.wiggle.lastframe = 3
    bpy.utils.unregister_class(WiggleScene)  # what Wiggle 2's unregister does, leaving Scene.wiggle
    removed = legacy.clean_dangling_wiggle2()
    check("Wiggle 2's dangling properties get removed", removed == ["Scene.wiggle"]
          and "wiggle" not in Scene.bl_rna.properties and scene.get("wiggle") is not None, f"{removed}")

    # a fake Wiggle 2 add-on: turning it off cleans up right away, no waiting on the timer
    import types

    class WiggleObject(bpy.types.PropertyGroup):
        mute: bpy.props.BoolProperty()

    def fake_register():
        bpy.utils.register_class(WiggleObject)
        bpy.types.Object.wiggle = bpy.props.PointerProperty(type=WiggleObject)

    def fake_unregister():
        bpy.utils.unregister_class(WiggleObject)

    fake = types.ModuleType("fake_wiggle_2")
    fake.bl_info = {"name": "Wiggle 2"}
    fake.register, fake.unregister = fake_register, fake_unregister
    sys.modules["fake_wiggle_2"] = fake
    try:
        fake.register()
        legacy.hook_wiggle2()
        legacy.hook_wiggle2()  # twice doesn't wrap twice
        fake.unregister()
        check("turning Wiggle 2 off removes its leftovers in the same call",
              "wiggle" not in bpy.types.Object.bl_rna.properties)
        legacy.unhook_wiggle2()
        check("and the hook comes off again", fake.unregister is fake_unregister)
    finally:
        del sys.modules["fake_wiggle_2"]

    # leftovers that slipped past the hook are gone before every save (they crashed a save with overrides)
    fake_register()
    bpy.utils.unregister_class(WiggleObject)
    check("a dangling Wiggle 2 property is there", "wiggle" in bpy.types.Object.bl_rna.properties)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(tempfile.mkdtemp(), "leftovers.blend"), copy=True)
    check("saving removes Wiggle 2 leftovers first", "wiggle" not in bpy.types.Object.bl_rna.properties)


def test_render_details():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.device = "CPU"
    scene.render.resolution_x = scene.render.resolution_y = 8
    scene.render.use_motion_blur = True
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.frame_end = 8
    scene.emils_wiggle.enabled = True
    enable(ob)
    # animated stiffness: renders have to use the animated value, not the viewport's
    w1 = ob.pose.bones["w1"].emils_wiggle.tail
    w1.stiff = 50.0
    ob.keyframe_insert('pose.bones["w1"].emils_wiggle.tail.stiff', frame=1)
    w1.stiff = 2000.0
    ob.keyframe_insert('pose.bones["w1"].emils_wiggle.tail.stiff', frame=8)
    viewport = {}
    for f in range(1, 9):
        scene.frame_set(f)
        viewport[f] = helper_mat(ob, "w2")
    rt = runtime.get(scene)
    runtime.reset_scene(scene)
    scene.frame_set(4)  # the viewport sits somewhere else, with another stiffness
    runtime.invalidate_scene(scene, force=True)
    for rig in rt.rigs.values():
        rig.ready = False
    seen = {}

    def spy(sc, dg):
        if dg is not None and dg.mode == "RENDER":
            seen.setdefault(sc.frame_current_final, helper_mat(ob, "w2"))
    bpy.app.handlers.frame_change_post.append(spy)
    rt.counts.clear()
    try:
        scene.render.filepath = os.path.join(tempfile.mkdtemp(), "mb_")
        bpy.ops.render.render(animation=True)
    finally:
        bpy.app.handlers.frame_change_post.remove(spy)
    whole = sorted(f for f in seen if f == int(f))
    subs = sorted(f for f in seen if f != int(f))
    check("motion blur steps are handled without resetting the sim",
          subs and rt.counts.get("SUB", 0) > 0 and rt.counts.get("RESET", 0) == 1,
          f"subframes {subs[:4]} counts {dict(rt.counts)}")
    worst = max(_mdiff(seen[f], viewport[int(f)]) for f in whole)
    check("render with animated settings and motion blur == viewport", worst < 1e-4, f"{worst:.2e}")

    # a render straight after opening a file
    path = os.path.join(tempfile.mkdtemp(), "reopen.blend")
    scene.render.use_motion_blur = False
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    scene = bpy.context.scene
    ob = bpy.data.objects["Rig"]
    seen.clear()

    def spy2(sc, dg):
        if dg is not None and dg.mode == "RENDER":
            seen[sc.frame_current] = helper_mat(ob, "w2")
    bpy.app.handlers.frame_change_post.append(spy2)
    try:
        scene.render.filepath = os.path.join(tempfile.mkdtemp(), "re_")
        bpy.ops.render.render(animation=True)
    finally:
        bpy.app.handlers.frame_change_post.remove(spy2)
    worst = max((_mdiff(seen[f], viewport[f]) for f in range(1, 9) if f in seen), default=1.0)
    check("rendering right after opening a file simulates", sorted(seen) == list(range(1, 9)) and worst < 1e-4,
          f"{sorted(seen)} {worst:.2e}")


def _mdiff(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(4) for j in range(4))


def test_fast_preview_and_loop():
    exact = {}
    fast = {}
    for label, store in (("exact", exact), ("fast", fast)):
        scene = fresh_scene()
        ob = make_chain("Rig")
        scene.emils_wiggle.enabled = True
        enable(ob)
        runtime.assume_playing = label == "fast"
        rt = runtime.get(scene)
        rt.counts.clear()
        for f in range(1, 41):
            scene.frame_set(f)
            store[f] = (helper_mat(ob, "w2"),
                        rt.rigs["Rig"].cache[f][1]["w2"][1].copy())
        store["counts"] = dict(rt.counts)
        if label == "fast":
            runtime.playback_stopped(scene)
            store["stopped"] = helper_mat(ob, "w2")
        runtime.assume_playing = None
    phys = max((exact[f][1] - fast[f][1]).length for f in range(1, 41))
    check("fast preview keeps the physics", phys < 1e-3, f"max tail diff {phys:.2e}")
    check("fast preview skips the second evaluation", fast["counts"].get("fast", 0) >= 35,
          f"{fast['counts']}")
    lag = max(_mdiff(fast[f][0], exact[f - 1][0]) for f in range(3, 41))
    check("fast preview shows the previous frame's wiggle", lag < 1e-3, f"{lag:.2e}")
    stop = _mdiff(fast["stopped"], exact[40][0])
    check("stopping playback shows the exact pose", stop < 1e-3, f"{stop:.2e}")

    # loop physics: once a loop repeats itself, playback replays the cache
    scene = fresh_scene()
    scene.frame_end = 40
    ob = make_chain("Rig", root_motion=((1, 0.0), (10, 1.0), (20, 0.0), (40, 0.0)))
    scene.emils_wiggle.enabled = True
    enable(ob, damp=3.0)
    runtime.assume_playing = True  # looping only counts during real playback
    rt = runtime.get(scene)
    rt.counts.clear()
    loops = 0
    for _loop in range(12):
        for f in range(1, 41):
            scene.frame_set(f)
        loops += 1
        if rt.counts.get("converged"):
            break
    check("looping physics settles into a repeat", rt.counts.get("converged", 0) >= 1, f"after {loops} loops")
    rt.counts.clear()
    for f in range(1, 41):
        scene.frame_set(f)
    check("a settled loop replays from the cache", rt.counts.get("SIM", 0) <= 1, f"{dict(rt.counts)}")
    runtime.assume_playing = None
    rt.counts.clear()
    scene.frame_set(1)
    check("jumping to the start outside playback reuses the cache", rt.counts.get("CACHE", 0) == 1,
          f"{dict(rt.counts)}")

    # Fast Preview shows every frame one late, cached ones too. Cached frames used to show on time,
    # so playing through a gap in the cache skipped a frame of wiggle and then repeated one.
    scene = fresh_scene()
    scene.emils_wiggle.loop = False
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 13))
    rig = runtime.get(scene).rigs["Rig"]
    ref = {f: rig.cache[f][1]["w2"][runtime.DELTA].copy() for f in range(1, 13)}
    del rig.cache[6], rig.cache[7]
    runtime.assume_playing = True
    shown = {}
    try:
        for f in range(1, 13):
            scene.frame_set(f)
            shown[f] = helper_mat(ob, "w2")
    finally:
        runtime.assume_playing = None
    runtime.playback_stopped(scene)
    lag = max(_mdiff(shown[f], ref[f - 1]) for f in range(2, 13))
    stopped = _mdiff(helper_mat(ob, "w2"), ref[12])
    check("fast preview is one frame late the whole way, through a gap in the cache",
          lag < 1e-5 and rig.cache[6][0] == rig.cache[8][0], f"{lag:.2e} {list(runtime.history)[-12:]}")
    check("and stopping on a cached frame shows it exactly", stopped < 1e-6, f"{stopped:.2e}")

    # renders stay exact even with fast preview on
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.frame_end = 6
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 8
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.emils_wiggle.enabled = True
    enable(ob)
    runtime.assume_playing = True
    written = {}

    def spy(sc, dg):
        if dg is not None and dg.mode == "RENDER":
            written[sc.frame_current] = helper_mat(ob, "w2")
    bpy.app.handlers.frame_change_post.append(spy)
    try:
        scene.render.filepath = os.path.join(tempfile.mkdtemp(), "f_")
        bpy.ops.render.render(animation=True)
    finally:
        bpy.app.handlers.frame_change_post.remove(spy)
        runtime.assume_playing = None
    worst = max(_mdiff(written[f], exact[f][0]) for f in range(2, 7))
    check("render ignores fast preview", worst < 1e-4, f"{worst:.2e}")


def test_threads_and_deferred_setup():
    import threading
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 4))
    rt = runtime.get(scene)
    seen = []
    t = threading.Thread(target=lambda: seen.append(runtime._can_edit_data(rt)))
    t.start()
    t.join()
    check("other threads (exports, compositor scenes) never create or delete anything",
          seen == [False] and runtime._can_edit_data(rt), f"{seen}")

    # a copied rig still points at the original's empties
    copy = ob.copy()
    copy.data = ob.data.copy()
    scene.collection.objects.link(copy)
    orig = set(_helper_ptrs(ob).values())
    probe = runtime.SceneRuntime()
    runtime.rebuild(scene, probe, edit=False)
    got = {b.helper.as_pointer() for b in probe.rigs[copy.name].bones if b.helper is not None}
    check("a read-only setup never hands a copy the original's empties", not got and probe.needs_edit,
          f"{len(got)} borrowed")
    check("and it didn't create anything", sum(1 for o in bpy.data.objects if runtime.HELPER_TAG in o) == 3)

    # the depsgraph update notices the new rig and a timer sets it up, no frame change needed
    bpy.context.evaluated_depsgraph_get()
    check("the new rig asks for its setup", rt.needs_edit and bpy.app.timers.is_registered(runtime._apply_edits))
    runtime._apply_edits()  # background Blender doesn't run timers by itself
    ptrs = _helper_ptrs(copy)
    check("the timer gives the copy its own empties",
          len(ptrs) == 3 and not set(ptrs.values()) & orig and not rt.needs_edit, f"{len(ptrs)}")
    play(scene, copy, range(4, 8))
    check("both rigs simulate", set(rt.rigs) == {"Rig", copy.name} and rt.counts.get("errors", 0) == 0,
          f"{set(rt.rigs)}")

    # a very long timeline keeps the cache inside its budget
    old = runtime.CACHE_BUDGET
    try:
        runtime.CACHE_BUDGET = 60  # 2 rigs x 3 bones: about 10 frames
        runtime.reset_scene(scene)
        play(scene, ob, range(1, 41))
        count, lo, hi = runtime.cache_info(scene)
        check("the cache drops far frames once it's full", count <= 10 and hi == 40, f"{count} ({lo}-{hi})")
        scene.frame_set(5)  # dropped, starts over there
        scene.frame_set(6)
        check("and a dropped frame just simulates again", rt.counts.get("errors", 0) == 0)
    finally:
        runtime.CACHE_BUDGET = old


def test_linked_and_overridden_rigs():
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    from EmilsWiggle import groups
    ob.pose.bones["w0"].emils_wiggle.group = groups.new_group(ob, "Lib").uid
    play(scene, ob, range(1, 4))
    path = os.path.join(tempfile.mkdtemp(), "lib.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    scene = fresh_scene()
    # a local object that happens to have the same name as one of the linked empties
    clash = bpy.data.objects.new("EmilsWiggle_Rig_w0", None)
    runtime.helper_collection().objects.link(clash)
    scene.emils_wiggle.enabled = True
    rt = runtime.get(scene)
    with bpy.data.libraries.load(path, link=True) as (_src, dst):
        dst.objects = ["Rig"]
    linked = dst.objects[0]
    scene.collection.objects.link(linked)
    play(scene, linked, range(1, 3))
    check("a linked rig without an override is left alone", not rt.rigs and rt.counts.get("errors", 0) == 0,
          f"{set(rt.rigs)} {dict(rt.counts)}")
    over = linked.override_create(remap_local_usages=True)
    moved = play(scene, over, range(1, 12))
    for mute in (True, False):  # the rig gets set up again each time
        over.pose.bones["w1"].emils_wiggle.mute = mute
        moved.update(play(scene, over, range(12, 16)))
    check("an overridden rig wiggles, even when a linked empty's name is taken locally",
          over.name in rt.rigs and rt.counts.get("errors", 0) == 0,
          f"{set(rt.rigs)} errors {rt.counts.get('errors', 0)}: {runtime.last_error.strip()[-150:]}")
    check("the local look-alike is still there", bpy.data.objects.get("EmilsWiggle_Rig_w0") is not None)

    bpy.context.view_layer.objects.active = over
    bpy.ops.object.mode_set(mode="POSE")
    s = over.emils_wiggle
    check("the linked file's Wiggle Group comes along", [g.name for g in s.groups] == ["Lib"])
    s.active_group = 0
    picked = sorted(pb.name for pb in over.pose.bones if pb.bone.select)
    for pb in over.pose.bones:
        pb.bone.select = pb.name == "w2"
    bpy.ops.emils_wiggle.group_add()
    added = [(g.name, [pb.name for pb in groups.members(over, g.uid)]) for g in s.groups]
    with groups.quiet():
        s.active_group = 0
    removed = bpy.ops.emils_wiggle.group_remove()
    check("on an overridden rig groups select, get added, and a linked one can't be removed (no error)",
          picked == ["w0"] and added == [("Lib", ["w0"]), ("Group", ["w2"])] and removed == {"CANCELLED"}
          and len(s.groups) == 2, f"{picked} {added} {removed}")
    bpy.ops.object.mode_set(mode="OBJECT")


def test_wiggle_groups():
    from EmilsWiggle import groups
    scene = fresh_scene()
    ob = make_chain("Rig", n=4)
    scene.emils_wiggle.enabled = True
    scene.emils_wiggle.edit_selected = True
    enable(ob)
    bpy.context.view_layer.objects.active = ob
    check("the group buttons only work in pose mode", not bpy.ops.emils_wiggle.group_add.poll())
    bpy.ops.object.mode_set(mode="POSE")
    bones = ob.pose.bones
    arm = ob.data
    s = ob.emils_wiggle

    def pick(*names):
        # like a click: rewriting the same selection would be an edit we can't tell apart
        for pb in bones:
            if pb.bone.select != (pb.name in names):
                pb.bone.select = pb.name in names
        if arm.bones.active != arm.bones[names[0]]:
            arm.bones.active = arm.bones[names[0]]

    def selected():
        return sorted(pb.name for pb in bones if pb.bone.select)

    def inside(g):
        return sorted(pb.name for pb in groups.members(ob, g.uid))

    def quietly_activate(index):
        with groups.quiet():
            s.active_group = index

    pick("w0", "w1")
    bpy.ops.emils_wiggle.group_add()
    pick("w2", "w3")
    bpy.ops.emils_wiggle.group_add()
    hair, tail = s.groups
    got = (hair.name, inside(hair), tail.name, inside(tail), s.active_group)
    check("adding a group puts the selected bones in it",
          got == ("Group", ["w0", "w1"], "Group.001", ["w2", "w3"], 1), f"{got}")
    hair.name, tail.name = "Hair", "Tail"

    pick("root")
    s.active_group = 0  # what clicking it in the list does
    check("clicking a group selects its bones, and only those, with one of them active",
          selected() == ["w0", "w1"] and arm.bones.active.name == "w0", f"{selected()} {arm.bones.active.name}")
    for pb in bones:
        pb.bone.select = False
    arm.bones.active = arm.bones["w1"]
    s.active_group = 0
    check("an active bone that's in the group stays active", arm.bones.active.name == "w1" and selected() == ["w0", "w1"])

    bones["w1"].emils_wiggle.tail.stiff = 77.0
    stiff = [round(bones[f"w{i}"].emils_wiggle.tail.stiff) for i in range(4)]
    check("editing the active bone then edits the whole group, nothing else", stiff == [77, 77, 200, 200],
          f"{stiff}")

    quietly_activate(1)
    pick("w1")
    bpy.ops.emils_wiggle.group_assign()
    check("a bone is in one group at most, assigning moves it",
          inside(hair) == ["w0"] and inside(tail) == ["w1", "w2", "w3"], f"{inside(hair)} {inside(tail)}")
    pick("w1", "w2")
    bpy.ops.emils_wiggle.group_unassign()
    check("removing takes the selected bones out", inside(tail) == ["w3"] and bones["w1"].emils_wiggle.group == 0)

    bones["w3"].bone.hide = True
    pick("w0")
    bpy.ops.emils_wiggle.group_select()
    check("hidden bones don't get selected", selected() == [] and arm.bones.active.name == "w0", f"{selected()}")
    bones["w3"].bone.hide = False
    bpy.ops.emils_wiggle.group_select()
    check("the Select button selects the group", selected() == ["w3"] and arm.bones.active.name == "w3")
    bpy.ops.emils_wiggle.group_deselect()
    check("and Deselect deselects it", selected() == [])

    tail.name = "Tips"
    bones["w3"].name = "tip"
    check("renaming the group or a bone keeps the bone in it", inside(tail) == ["tip"], f"{inside(tail)}")

    pick("w0", "tip")
    bpy.ops.emils_wiggle.copy()
    check("Copy Settings to Selected leaves the groups alone",
          bones["tip"].emils_wiggle.group == tail.uid and round(bones["tip"].emils_wiggle.tail.stiff) == 77)

    pick("w0")
    quietly_activate(0)
    bpy.ops.emils_wiggle.group_remove()
    check("removing a group keeps its bones, their settings and the selection",
          [g.name for g in s.groups] == ["Tips"] and bones["w0"].emils_wiggle.group == 0
          and round(bones["w0"].emils_wiggle.tail.stiff) == 77 and selected() == ["w0"] and s.active_group == 0)
    bones["w2"].emils_wiggle.group = 9  # pointing at a group that's gone
    new = groups.new_group(ob)
    check("a new group never takes a number bones still point at", new.uid == 10 and groups.find(ob, 9) is None,
          f"{new.uid}")
    quietly_activate(0)

    # selecting bones used to clear the whole cache (Blender tags the armature for it)
    def cached_after(edit):
        play(scene, ob, range(1, 11))
        edit()
        bpy.context.evaluated_depsgraph_get()
        return runtime.cache_info(scene)[0]

    def click_group():
        pick("root")
        s.active_group = 0

    def select_all():
        bpy.ops.pose.select_all(action="SELECT")

    def click_bone():
        pick("w1")

    def hide_bone():
        pick("w1")
        bpy.ops.pose.hide()

    def reveal_bones():
        bpy.ops.pose.reveal(select=False)

    def rename_group():
        s.groups[0].name += "x"

    def click_same_group():
        s.active_group = 0  # its bones are already the selection

    for label, edit in (("clicking a group", click_group), ("Select All", select_all),
                        ("clicking a bone", click_bone), ("hiding bones", hide_bone),
                        ("revealing them", reveal_bones), ("renaming a group", rename_group),
                        ("clicking the group that's already picked", click_same_group)):
        left = cached_after(edit)
        check(f"{label} keeps the cache", left == 10, f"{left}")

    def pose_bone():
        bones["w1"].location.x += 0.3

    def select_and_pose():
        pick("w2")
        bones["w2"].location.y += 0.2

    def select_and_move_rig():
        pick("w0")
        ob.location.x += 0.5

    def click_group_and_pose():
        s.active_group = 0
        bones["w1"].rotation_quaternion.x += 0.1

    def click_group_and_constraint():
        pick("root")
        s.active_group = 0
        c = bones["w1"].constraints.new("COPY_ROTATION")
        c.influence = 0.0

    def select_and_roll():
        bpy.ops.object.mode_set(mode="EDIT")
        arm.edit_bones["w2"].roll += 0.5
        for eb in arm.edit_bones:
            eb.select = eb.name == "w0"
        bpy.ops.object.mode_set(mode="POSE")

    for label, edit in (("posing a bone", pose_bone), ("selecting and posing at once", select_and_pose),
                        ("selecting and moving the rig at once", select_and_move_rig),
                        ("picking a group and posing at once", click_group_and_pose),
                        ("picking a group and adding a constraint at once", click_group_and_constraint),
                        ("selecting and rolling a bone in edit mode", select_and_roll)):
        left = cached_after(edit)
        check(f"{label} still clears the cache", left == 0, f"{left}")
    bpy.ops.object.mode_set(mode="OBJECT")
    left = cached_after(click_bone)
    check("in object mode a selection change still counts as an edit (nothing to compare with)", left == 0,
          f"{left}")
    check("no errors", runtime.get(scene).counts.get("errors", 0) == 0, runtime.last_error[-200:])


def _entry_diff(a, b):
    """Largest difference between two cache entries (wiggle offset and tail position of every bone)."""
    worst = 0.0
    for name, snap in a[1].items():
        other = b[1][name]
        worst = max(worst, _mdiff(snap[runtime.DELTA], other[runtime.DELTA]), (snap[1] - other[1]).length)
    return worst


def _plane(name, size, z):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(-size, -size, z), (size, -size, z), (size, size, z), (-size, size, z)], [], [(0, 1, 2, 3)])
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def test_background_cache():
    from EmilsWiggle import background
    scene = fresh_scene()
    scene.use_gravity = True
    scene.frame_end = 40
    s = scene.emils_wiggle
    s.substeps, s.preroll = 2, 5
    mover = bpy.data.objects.new("Mover", None)
    scene.collection.objects.link(mover)
    mover.keyframe_insert("location", frame=1)
    mover.location.x = 1.5
    mover.keyframe_insert("location", frame=30)
    ob = make_chain("Rig")
    ob.parent = mover
    floor = _plane("Floor", 3.0, 2.2)
    wind = bpy.data.objects.new("Wind", None)
    scene.collection.objects.link(wind)
    with bpy.context.temp_override(active_object=wind, object=wind):
        bpy.ops.object.forcefield_toggle()
    wind.field.type = "WIND"
    wind.field.strength = 20.0
    wind.rotation_euler = (1.2, 0.0, 0.4)
    s.enabled = True
    enable(ob, stiff=80.0, damp=2.0, gravity=1.0, collider_type="Object", collider=floor, radius=0.2,
           wind_ob=wind, wind=1.0)
    w1 = ob.pose.bones["w1"].emils_wiggle.tail
    w1.stiff = 50.0
    ob.keyframe_insert('pose.bones["w1"].emils_wiggle.tail.stiff', frame=1)
    w1.stiff = 400.0
    ob.keyframe_insert('pose.bones["w1"].emils_wiggle.tail.stiff', frame=30)

    # the reference: playing the range from the start
    rt = runtime.get(scene)
    play(scene, ob, range(1, 41))
    rig = rt.rigs["Rig"]
    ref = dict(rig.cache)
    check("the reference collides and blows in the wind", rig.colliders == {"Floor"} and rig.winds == {"Wind"},
          f"{rig.colliders} {rig.winds}")

    # forget it and sit somewhere in the middle, like after an edit
    runtime.invalidate_scene(scene, force=True)
    scene.frame_set(25)
    helpers = {n: helper_mat(ob, n) for n in ("w0", "w1", "w2")}
    places = {o.name: o.matrix_world.copy() for o in (mover, floor, wind, ob)}
    done = background.run_blocking(scene)
    key = runtime.reset_key(1, s)
    filled = sorted(f for f, e in rig.cache.items() if e[0] == key and not e[2])
    check("the background cache fills the whole range", done == 40 and filled == list(range(1, 41)),
          f"{done} frames, {len(filled)} with the playback key, reason '{rig.bg_reason}'")
    worst = max((_entry_diff(rig.cache[f], ref[f]) for f in range(1, 41) if f in rig.cache), default=1.0)
    check("background frames == playing from the start", worst < 1e-4, f"{worst:.2e}")
    check("nothing real moved while it worked",
          all(_mdiff(helper_mat(ob, n), m) < 1e-9 for n, m in helpers.items())
          and all(_mdiff(o.matrix_world, places[o.name]) < 1e-9 for o in (mover, floor, wind, ob)))
    sc = background.cache_scene()
    check("the hidden scene is left empty and hidden from the scene list",
          sc is not None and sc.name.startswith(".") and len(sc.objects) == 0
          and not any(background.COPY_TAG in o for o in bpy.data.objects))
    rt.counts.clear()
    again = play(scene, ob, range(1, 41))
    check("playing afterwards only reads the cache", not rt.counts.get("SIM") and not rt.counts.get("RESET"),
          f"{dict(rt.counts)}")

    # it picks up where the cache stops
    for f in range(21, 41):
        del rig.cache[f]
    rig.bg_done = None
    done = background.run_blocking(scene)
    worst = max(_entry_diff(rig.cache[f], ref[f]) for f in range(21, 41))
    check("it carries on from the last cached frame", done == 20 and worst < 1e-4, f"{done} frames, {worst:.2e}")
    check("the viewport still shows the same wiggle", max_diff(play(scene, ob, range(1, 41)), again) < 1e-6)

    # Gravity turned off after the hidden scene was made: it used to keep simulating with gravity,
    # and a background frame between two played ones made the bones dip for a frame.
    scene.use_gravity = False
    scene.render.fps = 24
    play(scene, ob, range(1, 41))
    ref_off = dict(rig.cache)
    for f in range(21, 41):
        del rig.cache[f]
    rig.bg_done = None
    done = background.run_blocking(scene)
    worst = max(_entry_diff(rig.cache[f], ref_off[f]) for f in range(21, 41))
    check("the background cache follows gravity and fps changes", done == 20 and worst < 1e-4,
          f"{done} frames, {worst:.2e}")
    scene.use_gravity = True
    scene.render.fps = 30
    play(scene, ob, range(1, 41))

    # a collider that rides a wiggle bone would differ in the hidden scene, so it's skipped
    floor.parent = ob
    floor.parent_type = "BONE"
    floor.parent_bone = "w0"
    runtime.invalidate_scene(scene, force=True)
    scene.frame_set(3)
    bpy.context.evaluated_depsgraph_get()  # the edit gets noticed...
    scene.frame_set(4)  # ...and the next frame reads the settings again
    done = background.run_blocking(scene)
    check("a collider riding a wiggle bone makes it skip that rig",
          done == 0 and "moves with wiggling bones" in rig.bg_reason, f"{done} '{rig.bg_reason}'")
    check("and the panel says why", "Floor" in background.status(scene), background.status(scene))
    floor.parent = None

    # a driver reading the real scene would read another frame than the hidden scene evaluates
    fc = ob.pose.bones["root"].driver_add("rotation_quaternion", 1)
    var = fc.driver.variables.new()
    var.type = "SINGLE_PROP"
    var.targets[0].id_type = "SCENE"
    var.targets[0].id = scene
    var.targets[0].data_path = "frame_current"
    fc.driver.expression = var.name + " * 0.001"
    runtime.invalidate_scene(scene, force=True)
    scene.frame_set(5)
    done = background.run_blocking(scene)
    check("a driver reading the scene makes it skip that rig", done == 0 and "scene" in rig.bg_reason,
          f"{done} '{rig.bg_reason}'")
    ob.pose.bones["root"].driver_remove("rotation_quaternion", 1)

    background.remove_all()
    check("saving removes the hidden scene", background.cache_scene() is None)


def test_dropped_frames():
    scene = fresh_scene()
    scene.frame_end = 60
    ob = make_chain("Rig")
    s = scene.emils_wiggle
    s.loop = False
    s.enabled = True
    enable(ob)
    rt = runtime.get(scene)
    exact = play(scene, ob, range(1, 21))
    runtime.invalidate_scene(scene, force=True)

    # slow playback: every frame drawn skips a few
    runtime.assume_playing = True
    try:
        scene.frame_set(1)
        rt.counts.clear()
        for f in (2, 3, 10, 17):
            scene.frame_set(f)
        rig = rt.rigs["Rig"]
        check("dropped frames during playback don't restart the sim",
              not rt.counts.get("RESET") and rt.counts.get("SIM") == 4, f"{dict(rt.counts)}")
        check("frames after a skip are marked as guessed", rig.cache[10][2] and rig.cache[17][2]
              and not rig.cache[3][2])
        # playback loops back while still dropping frames: go on from the cached first frame
        rt.counts.clear()
        scene.frame_set(4)
        check("looping with dropped frames carries on from the first frame",
              not rt.counts.get("RESET") and rt.counts.get("SIM") == 1
              and runtime.history[-1][2].startswith("sim, from frame 1"), f"{dict(rt.counts)} {runtime.history[-1]}")
    finally:
        runtime.assume_playing = None

    # an exact pass replaces the guesses, and guesses never replace exact frames
    after = play(scene, ob, range(1, 21))
    check("exact frames replace guessed ones", not any(rig.cache[f][2] for f in range(1, 21))
          and max_diff(after, exact) < 1e-5, f"{max_diff(after, exact):.2e}")
    runtime.assume_playing = True
    try:
        scene.frame_set(1)
        scene.frame_set(9)
        shown = _mdiff(rig.by_name["w2"].delta, rig.cache[9][1]["w2"][runtime.DELTA])
        check("a skip lands on the exact cached frame instead", not rig.cache[9][2] and shown < 1e-5,
              f"approx {rig.cache[9][2]}, shown {shown:.2e}, {runtime.history[-1]}")
        for f in range(5, 9):
            del rig.cache[f]
        scene.frame_set(1)
        scene.frame_set(6)
        scene.frame_set(9)
        check("guesses go back onto the exact run they came from",
              runtime.history[-1][2].startswith("cache, back on the exact run"), f"{runtime.history[-1]}")
    finally:
        runtime.assume_playing = None

    # The rig sits on its own run (it started from rest on frame 30) while the background cache fills
    # the range from the first frame. Slow playback from there used to jump onto those frames, which
    # looks just like the wiggle resetting in the middle of the animation.
    from EmilsWiggle import background
    runtime.playback_stopped(scene)
    runtime.invalidate_scene(scene, force=True)
    scene.frame_set(30)
    background.run_blocking(scene)
    runtime.assume_playing = True
    try:
        rt.counts.clear()
        scene.frame_set(35)
        scene.frame_set(40)
        check("guesses never jump onto another run's frames", not rt.counts.get("CACHE")
              and rt.counts.get("SIM") == 2 and not rig.cache[40][2], f"{dict(rt.counts)} {runtime.history[-1]}")

        # one frame taking longer than a second skips more frames than the fps, still the same playback
        runtime.playback_stopped(scene)
        scene.frame_end = 200
        runtime.invalidate_scene(scene, force=True)
        scene.frame_set(1)
        scene.frame_set(2)
        rt.counts.clear()
        scene.frame_set(50)
        check("a frame that takes over a second doesn't restart the sim",
              not rt.counts.get("RESET") and rt.counts.get("SIM") == 1, f"{dict(rt.counts)} {runtime.history[-1]}")
        runtime.playback_stopped(scene)
        scene.frame_set(120)
        check("starting playback far from the last frame still starts over, and the report says why",
              rt.counts.get("RESET") == 1 and runtime.history[-1][2] == "reset (playback went from frame 50 to 120)",
              f"{dict(rt.counts)} {runtime.history[-1]}")
    finally:
        runtime.assume_playing = None
        runtime.playback_stopped(scene)
        scene.frame_end = 60

    # Frame Step renders
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 8
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.frame_end = 30
    scene.frame_step = 6
    runtime.reset_scene(scene)
    scene.frame_set(1)  # the render comes back to this frame at the end
    rt.counts.clear()
    try:
        scene.render.filepath = os.path.join(tempfile.mkdtemp(), "step_")
        bpy.ops.render.render(animation=True)
    finally:
        scene.frame_step = 1
    check("a render with Frame Step 6 keeps simulating between frames",
          not rt.counts.get("RESET") and rt.counts.get("SIM", 0) == 4, f"{dict(rt.counts)}")


def test_pose_not_linked_yet():
    """Right after an undo a pose bone can have no bone until Blender rebuilds the pose."""
    import types
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 4))
    rt = runtime.get(scene)
    rig = rt.rigs["Rig"]
    fake = types.SimpleNamespace(pose=types.SimpleNamespace(bones=[types.SimpleNamespace(bone=None)]))
    check("a pose that isn't linked yet has no signature", runtime._rig_signature(fake) is None)
    real = runtime._rig_signature
    runtime._rig_signature = lambda o: None
    try:
        errors = rt.counts.get("errors", 0)
        runtime.rebuild(scene, rt, edit=True)
        kept = rt.rigs.get("Rig") is rig and rt.structure_dirty and rt.needs_edit
        check("the rig is kept and looked at again later, no error", kept and rt.counts.get("errors", 0) == errors
              and runtime.find_constraint(ob.pose.bones["w1"]) is not None,
              f"kept {kept}, errors {rt.counts.get('errors', 0) - errors}")
    finally:
        runtime._rig_signature = real
    runtime._edit_now(scene, rt)
    check("and once it is, everything's back to normal", not rt.structure_dirty and not rt.needs_edit
          and rt.rigs.get("Rig") is rig)


def test_change_mid_run():
    """Gravity turned off while the chain hangs bent: the frames after that spring back, and a later
    loop from the first frame used to replay them in the middle of its own run (a twitch)."""
    scene = fresh_scene()
    scene.use_gravity = True
    scene.gravity = (6.0, 0.0, -9.81)  # sideways, so it bends the upright chain
    scene.emils_wiggle.loop = False
    try:
        ob = make_chain("Rig", root_motion=((1, 0.0),))
        scene.emils_wiggle.enabled = True
        enable(ob)
        bent = play(scene, ob, range(1, 21))
        scene.use_gravity = False
        play(scene, ob, range(21, 31))
        straight = play(scene, ob, range(1, 31))
        worst = max((straight[f] - straight[1]).length for f in range(1, 31))
        check("frames simulated right after a change aren't replayed as the run from the start",
              (bent[20] - bent[1]).length > 0.05 and worst < 1e-5, f"bent {(bent[20] - bent[1]).length:.3f}, "
              f"twitch {worst:.2e}, {list(runtime.history)[-12:]}")
    finally:
        scene.gravity = (0.0, 0.0, -9.81)


def test_settings_warnings():
    scene = fresh_scene()
    scene.use_gravity = True
    scene.render.fps = 24
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    tail = ob.pose.bones["w2"].emils_wiggle.tail

    def found():
        return [(icon, f"{title} {detail}") for icon, title, detail in runtime.side_warnings(scene, tail)]

    def texts():
        return [t for _icon, t in found()]
    check("sane settings have no warnings", texts() == [], f"{texts()}")

    tail.gravity = 1e30
    check("on a tail that can't stretch that's only a note",
          [i for i, t in found() if "Gravity" in t] == ["INFO"])
    tail.stretch = 1.0
    check("a stretchy one gets a real warning",
          [i for i, t in found() if "Gravity" in t] == ["ERROR"])
    play(scene, ob, range(1, 5))
    rig = runtime.get(scene).rigs["Rig"]
    check("a gravity like that blows the sim up and the rig remembers which bone",
          rig.blowups > 0 and "w2" in rig.blowup_bones and rig.blowup_frame is not None,
          f"{rig.blowups} {rig.blowup_bones} {rig.blowup_frame}")
    check("and that bone's settings say why", any("Gravity" in t for t in texts()), f"{texts()}")
    tail.gravity = 1.0
    check("changing a setting clears the alert", rig.blowups == 0 and not rig.blowup_bones)
    tail.stretch = 0.0

    plane = _plane("Wall", 1.0, 0.0)
    tail.collider_type = "Object"
    tail.collider = plane
    tail.bounce, tail.friction = 3.0, 2.0
    hits = texts()
    check("bounce and friction over 1 get flagged when colliding",
          any("Bounce" in t for t in hits) and any("Friction" in t for t in hits), f"{hits}")
    tail.collider = None
    check("but not without a collider", not any("Bounce" in t for t in texts()))
    tail.bounce, tail.friction = 0.5, 0.5

    wind = bpy.data.objects.new("Gust", None)
    scene.collection.objects.link(wind)
    with bpy.context.temp_override(active_object=wind, object=wind):
        bpy.ops.object.forcefield_toggle()
    wind.field.type = "WIND"
    wind.field.strength = 500.0
    tail.wind_ob = wind
    tail.wind, tail.mass = 1.0, 0.01
    check("strong wind on a light end gets flagged", any("Wind" in t for t in texts()), f"{texts()}")
    tail.wind_ob = None
    tail.mass = 1.0

    tail.stiff, tail.damp = 1e6, 1000.0
    notes = found()
    check("stiffness and damping past what they can do get an info note",
          [i for i, t in notes if "Stiff" in t] == ["INFO"] and [i for i, t in notes if "Damp" in t] == ["INFO"],
          f"{notes}")
    tail.per_axis = True
    tail.stiff_axis = (1.0, 1.0, 1e6)
    check("per axis values are checked too", any("Stiff" in t for t in texts()))
    check("a zero scale is spotted", runtime.zero_scale(Matrix.Diagonal((1.0, 0.0, 1.0, 1.0)))
          and not runtime.zero_scale(ob.matrix_world))


def test_huge_values():
    from mathutils import Matrix
    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    play(scene, ob, range(1, 4))
    rt = runtime.get(scene)
    rig = rt.rigs["Rig"]
    b = rig.by_name["w2"]
    keep = b.delta.copy()
    # finite, but Blender's float32 decomposition turns a scale like this into inf
    b.delta = Matrix.Diagonal((1.0, 1e20, 1.0, 1.0))
    check("a huge wiggle offset counts as blown up", not runtime._all_finite(rig))
    before = runtime.bad_writes
    writer = runtime.HelperWriter()
    b.shown = None
    writer.show(b, b.delta)
    writer.flush()
    helper = runtime.find_constraint(ob.pose.bones["w2"]).target
    values = list(helper.location) + list(helper.rotation_quaternion) + list(helper.scale)
    check("and an empty never gets inf or nan from it", all(math.isfinite(v) for v in values)
          and runtime.bad_writes == before + 1, f"{values}")
    b.delta = keep
    b.shown = None


def _preroll_steps(rig_name):
    """(done, total) from the last "preroll x/y steps" history note of a rig."""
    for _f, name, what in reversed(runtime.history):
        if name == rig_name and "preroll " in what:
            done, total = what.split("preroll ")[1].split(" ")[0].split("/")
            return int(done), int(total)
    return None


def test_preroll():
    from EmilsWiggle import solver
    out = {}
    cases = (
        ("none", 0, None, 50.0, 10.0),
        ("early", 400, None, 50.0, 10.0),
        ("full", 400, 10 ** 9, 50.0, 10.0),  # a window that never ends = the old full preroll
        ("swing", 400, None, 0.0, 0.0),  # no spring, no damping: it never stops moving
    )
    try:
        for label, preroll, window, stiff, damp in cases:
            scene = fresh_scene()
            scene.use_gravity = True
            scene.gravity = (6.0, 0.0, -9.81)
            s = scene.emils_wiggle
            s.preroll, s.substeps = preroll, 2
            ob = make_chain("Rig", root_motion=((1, 0.0),))
            s.enabled = True
            enable(ob, stiff=stiff, damp=damp, gravity=1.0)
            solver.SETTLE_WINDOW = window or 10
            scene.frame_set(1)
            out[label] = (tail_world(ob).copy(), _preroll_steps("Rig"))
    finally:
        solver.SETTLE_WINDOW = 10
        bpy.context.scene.gravity = (0.0, 0.0, -9.81)
    sag = (out["early"][0] - out["none"][0]).length
    check("preroll lets the chain settle before the first frame", sag > 0.05, f"moved {sag:.3f}")
    steps = out["early"][1]
    check("preroll stops once nothing moves", steps is not None and steps[0] < steps[1], f"{steps}")
    same = (out["early"][0] - out["full"][0]).length
    check("stopping the preroll early gives the same pose", same < 1e-3,
          f"diff {same:.2e}, full {out['full'][1]}")
    check("a chain that keeps swinging gets the whole preroll", out["swing"][1] == (800, 800),
          f"{out['swing'][1]}")


def test_perf():
    out = {}
    for label in ("off", "muted", "on", "fast", "cached"):
        scene = fresh_scene()
        scene.frame_end = 120
        rigs = []
        for r in range(3):
            ob = make_chain(f"Rig{r}", n=20)
            ob.location.x = r * 3
            rigs.append(ob)
            bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, location=(r * 3, 0, 10))
            me = bpy.context.object
            me.parent = ob
            mod = me.modifiers.new("Arm", "ARMATURE")
            mod.object = ob
            for pb in ob.pose.bones:
                vg = me.vertex_groups.new(name=pb.name)
                vg.add(list(range(0, len(me.data.vertices), 3)), 0.5, "REPLACE")
            me.modifiers.new("Sub", "SUBSURF").levels = 1
        if label != "off":
            scene.emils_wiggle.enabled = True
            for ob in rigs:
                enable(ob)
                if label == "muted":
                    ob.emils_wiggle.mute = True
        scene.emils_wiggle.loop = label != "cached"
        runtime.assume_playing = label == "fast"
        scene.frame_set(1)
        if label == "cached":
            for f in range(1, 121):
                scene.frame_set(f)
        t0 = time.perf_counter()
        for f in range(1, 121):
            scene.frame_set(f)
        out[label] = (time.perf_counter() - t0) / 120 * 1000
        runtime.assume_playing = None
    return out


def run(fn, *args):
    print(f"--- {fn.__name__}", flush=True)
    t0 = time.perf_counter()
    out = fn(*args)
    print(f"    done in {time.perf_counter() - t0:.1f}s", flush=True)
    return out


def main():
    perf = run(test_perf)  # first: a GPU render turns on GPU subdivision and skews timings
    rest, wig = run(test_basic_and_settle)
    run(test_fps_base)
    run(test_cache_scrub)
    run(test_cache_invalidation)
    run(test_mute_and_manual_pose)
    run(test_axis_lock_and_per_axis, rest, wig)
    run(test_scaled_armature)
    run(test_collision_pin_head)
    run(test_render_live_and_cached)
    run(test_save_load)
    run(test_import_and_copy_and_bake)
    run(test_fast_preview_and_loop)
    run(test_ui_draw)
    run(test_robustness)
    run(test_structure_changes)
    run(test_scene_copies)
    run(test_caches_and_updates)
    run(test_render_details)
    run(test_preroll)
    run(test_threads_and_deferred_setup)
    run(test_linked_and_overridden_rigs)
    run(test_background_cache)
    run(test_dropped_frames)
    run(test_huge_values)
    run(test_settings_warnings)
    run(test_change_mid_run)
    run(test_pose_not_linked_yet)
    run(test_wiggle_groups)
    from EmilsWiggle import handlers
    check("no errors inside the handlers during the whole run (empty-mesh collider included)",
          handlers.error_count == 0, f"{handlers.error_count} errors, see the log")
    return perf


perf = {}
try:
    perf = main()
except Exception:
    results.append(("FAIL", "crashed", traceback.format_exc()))

lines = [f"Blender {bpy.app.version_string}"]
lines += [f"{s}  {n}" + (f"  [{d}]" if d else "") for s, n, d in results]
if perf:
    lines.append("perf ms/frame: " + ", ".join(f"{k}={v:.2f}" for k, v in perf.items()))
fails = sum(1 for s, _n, _d in results if s == "FAIL")
lines.append(f"{len(results) - fails} passed, {fails} failed")
_log.flush()
with open(RESULT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os._exit(0)
