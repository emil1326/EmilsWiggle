"""
Headless tests for Emil's Wiggle. Run with Blender 3.6:

    blender -b --factory-startup --python EmilsWiggle/tests/run_tests.py -- <result.txt>

Results (and any traceback) are written to <result.txt>.
"""

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
from mathutils import Vector  # noqa: E402

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
    frames = range(1, 121)
    rest = play(scene, ob, frames)
    scene.emils_wiggle.enabled = True
    enable(ob)
    wig = play(scene, ob, frames)
    check("wiggle moves the chain", max_diff(wig, rest) > 0.05, f"max diff {max_diff(wig, rest):.4f}")
    settled = (wig[120] - rest[120]).length
    check("chain settles back to rest", settled < 0.005, f"diff at 120: {settled:.5f}")
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
    check("per axis gets seeded from the single value", stiff == (400.0, 400.0, 400.0), f"{stiff}")

    scene = fresh_scene()
    ob = make_chain("Rig")
    scene.emils_wiggle.enabled = True
    enable(ob)
    for pb in ob.pose.bones:
        if pb.name.startswith("w"):
            t = pb.emils_wiggle.tail
            t.per_axis = True
            t.stiff_axis = (4000.0, 400.0, 400.0)
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
    s.tail.per_axis = True
    s.tail.collider = plane
    s.head.collider_type = "Collection"
    s.head.collider_collection = scene.collection.children[0] if scene.collection.children else None
    play(scene, ob, range(1, 4))
    draw_all(ctx)
    ob.emils_wiggle.mute = True
    draw_all(ctx)
    ob.emils_wiggle.mute = False
    ob.emils_wiggle.freeze = True
    draw_all(ctx)
    ob.emils_wiggle.freeze = False
    draw_all(types.SimpleNamespace(scene=scene, object=None, active_pose_bone=None, mode="OBJECT",
                                   selected_pose_bones=None))
    check("every panel draws without bad names", not errors, "; ".join(sorted(set(errors)))[:300])
    report = debug.build_report(bpy.context)
    print(report)
    check("debug report lists the rig, its bones and recent frames",
          "Rig: 1 bone (1 tail, 1 head)" in report and "w1 (wiggle parent -" in report
          and "tail: mass" in report and "head: mass" in report and "recent frames:" in report)
    debug.force_show = False
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
