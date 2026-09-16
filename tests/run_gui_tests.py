"""
GUI test for Emil's Wiggle: real viewport playback and threaded F12 / Ctrl+F12 renders
(the parts run_tests.py can't reach in background mode). Opens its own Blender window,
runs by itself and quits. Run with Blender 3.6:

    blender --factory-startup --no-window-focus --python EmilsWiggle/tests/run_gui_tests.py -- <result.txt>
"""
import os
import sys
import tempfile
import time
import traceback

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
RESULT = argv[0] if argv else os.path.join(tempfile.gettempdir(), "emils_wiggle_gui_tests.txt")
log = open(RESULT + ".log", "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
import faulthandler
faulthandler.enable(log)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import bpy
from mathutils import Vector

import EmilsWiggle
from EmilsWiggle import runtime, handlers
EmilsWiggle.register()

OUT = tempfile.mkdtemp(prefix="emils_wiggle_gui_")
results = []


def check(name, cond, detail=""):
    results.append(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    print(results[-1], flush=True)


def finish():
    results.append(f"handler errors: {handlers.error_count}")
    with open(RESULT, "w", encoding="utf-8") as f:
        f.write("\n".join(results) + "\n")
    os._exit(0)


bpy.context.preferences.view.render_display_type = "NONE"
scene = bpy.context.scene
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)

arm = bpy.data.armatures.new("Rig")
rig = bpy.data.objects.new("Rig", arm)
scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
prev = arm.edit_bones.new("root")
prev.head, prev.tail = (0, 0, 0), (0, 0, 1)
for i in range(3):
    eb = arm.edit_bones.new(f"w{i}")
    eb.head, eb.tail = prev.tail, prev.tail + Vector((0, 0, 1))
    eb.parent = prev
    eb.use_connect = True
    prev = eb
bpy.ops.object.mode_set(mode="OBJECT")
root = rig.pose.bones["root"]
for frame, x in ((1, 0.0), (8, 2.0), (16, 0.0)):
    root.location = (x, 0, 0)
    root.keyframe_insert("location", frame=frame)
rig.keyframe_insert("location", frame=1)
rig.location.z = 1.5
rig.keyframe_insert("location", frame=24)
mover = bpy.data.objects.new("Mover", None)
scene.collection.objects.link(mover)
mover.keyframe_insert("rotation_euler", frame=1)
mover.rotation_euler.z = 0.4
mover.keyframe_insert("rotation_euler", frame=24)
rig.parent = mover
bpy.ops.mesh.primitive_cube_add(size=0.8)
cube = bpy.context.object
cube.parent = rig
cube.parent_type = "BONE"
cube.parent_bone = "w2"
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
scene.collection.objects.link(cam)
cam.location = (1, -9, 3)
cam.rotation_euler = (1.5708, 0, 0)
scene.camera = cam
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
scene.frame_start, scene.frame_end = 1, 24
scene.render.fps = 24

scene.emils_wiggle.enabled = True
for i in range(3):
    rig.pose.bones[f"w{i}"].emils_wiggle.use_tail = True
check("lock interface got turned on", scene.render.use_lock_interface)


def helper_mat(name):
    return runtime.find_constraint(rig.pose.bones[name]).target.matrix_basis.copy()


def mdiff(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(4) for j in range(4))


# exact reference from the viewport, frame by frame, not playing
ref = {}
runtime.reset_scene(scene)
for f in range(1, 25):
    scene.frame_set(f)
    ref[f] = helper_mat("w2")

spy_log = {}


def spy(sc, dg):
    if dg is not None and dg.mode == "RENDER":
        spy_log.setdefault(sc.frame_current, []).append(helper_mat("w2"))


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type in {"VIEW_3D", "DOPESHEET_EDITOR", "TIMELINE"})
    region = next(r for r in area.regions if r.type == "WINDOW")
    return dict(window=win, screen=win.screen, area=area, region=region, scene=scene)


def pixels(path):
    img = bpy.data.images.load(path)
    px = list(img.pixels)
    bpy.data.images.remove(img)
    return px


def steps():
    rt = runtime.get(scene)
    # --- real playback
    runtime.reset_scene(scene)
    scene.frame_set(1)
    rt.counts.clear()
    with bpy.context.temp_override(**ctx()):
        bpy.ops.screen.animation_play()
    t0 = time.time()
    while time.time() - t0 < 3.0:
        yield 0.1
    playing = runtime._is_playing()
    with bpy.context.temp_override(**ctx()):
        bpy.ops.screen.animation_cancel(restore_frame=False)
    yield 0.3
    check("playback is detected", playing)
    check("fast preview used while playing", rt.counts.get("fast", 0) > 5, f"{dict(rt.counts)}")
    b = rt.rigs["Rig"].by_name["w2"]
    check("stopping shows the exact wiggle", mdiff(helper_mat("w2"), b.delta) < 1e-5,
          f"{mdiff(helper_mat('w2'), b.delta):.2e}")
    check("not playing after stop", not runtime._is_playing())

    # --- live Ctrl+F12, nothing cached
    runtime.reset_scene(scene)
    scene.frame_set(5)
    runtime.invalidate_scene(scene, force=True)
    for rig_rt in rt.rigs.values():
        rig_rt.ready = False
    bpy.app.handlers.frame_change_post.append(spy)
    scene.render.filepath = os.path.join(OUT, "live_")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    yield 0.5
    t0 = time.time()
    while bpy.app.is_job_running("RENDER") and time.time() - t0 < 120:
        yield 0.25
    bpy.app.handlers.frame_change_post.remove(spy)
    frames = sorted(spy_log)
    check("threaded render ran our handlers", frames == list(range(1, 25)), f"{frames}")
    worst = max((mdiff(m, ref[f]) for f in frames for m in spy_log[f]), default=1.0)
    check("threaded live render == viewport sim", worst < 1e-4, f"{worst:.2e}")
    check("rendering flag cleared after the job", not rt.rendering)
    yield 0.5

    # --- cached Ctrl+F12
    runtime.reset_scene(scene)
    for f in range(1, 25):
        scene.frame_set(f)
    scene.frame_set(10)
    rt.counts.clear()
    scene.render.filepath = os.path.join(OUT, "cached_")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    yield 0.5
    t0 = time.time()
    while bpy.app.is_job_running("RENDER") and time.time() - t0 < 120:
        yield 0.25
    yield 0.5
    check("cached render only replayed", rt.counts.get("SIM", 0) == 0 and rt.counts.get("CACHE", 0) >= 24,
          f"{dict(rt.counts)}")
    same = all(pixels(os.path.join(OUT, f"live_{f:04d}.png")) == pixels(os.path.join(OUT, f"cached_{f:04d}.png"))
               for f in (1, 6, 12, 18, 24))
    check("threaded live and cached renders are pixel identical", same)

    # --- F12 on a cached frame
    scene.frame_set(12)
    scene.render.filepath = os.path.join(OUT, "still_12.png")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
    yield 0.5
    t0 = time.time()
    while bpy.app.is_job_running("RENDER") and time.time() - t0 < 60:
        yield 0.25
    yield 0.5
    check("F12 matches the animation frame",
          pixels(os.path.join(OUT, "still_12.png")) == pixels(os.path.join(OUT, "cached_0012.png")))
    check("frame stayed where it was", scene.frame_current == 12)


_gen = steps()


def tick():
    try:
        return next(_gen)
    except StopIteration:
        finish()
    except Exception:
        results.append("CRASHED " + traceback.format_exc())
        finish()
    return None


bpy.app.timers.register(tick, first_interval=1.0)


def watchdog():
    results.append("TIMEOUT")
    finish()


bpy.app.timers.register(watchdog, first_interval=240.0)
