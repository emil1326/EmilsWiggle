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
    ob = bpy.data.objects["Rig"]  # looked up every time, the file gets reopened later
    return runtime.find_constraint(ob.pose.bones[name]).target.matrix_basis.copy()


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
    return dict(window=win, screen=win.screen, area=area, region=region, scene=win.scene)


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

    # --- Ctrl+F12 straight after opening the file: the rig gets set up on the render thread
    path = os.path.join(OUT, "reopen.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    yield 1.0
    reopened = bpy.context.scene
    check("nothing is set up before the render", not runtime.get(reopened).rigs)
    spy_log.clear()
    bpy.app.handlers.frame_change_post.append(spy)
    reopened.render.filepath = os.path.join(OUT, "reopen_")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    yield 0.5
    t0 = time.time()
    while bpy.app.is_job_running("RENDER") and time.time() - t0 < 120:
        yield 0.25
    yield 0.5
    if spy in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(spy)
    frames = sorted(spy_log)
    worst = max((mdiff(m, ref[f]) for f in frames for m in spy_log[f]), default=1.0)
    check("render right after opening the file simulates like the viewport",
          frames == list(range(1, 25)) and worst < 1e-4, f"{len(frames)} frames, {worst:.2e}")
    check("and it didn't create anything from the render thread",
          sum(1 for o in bpy.data.objects if runtime.HELPER_TAG in o) == 3)

    # --- background cache: sit still for a moment and the range fills up by itself
    from EmilsWiggle import background
    here = bpy.context.scene
    rt2 = runtime.get(here)
    runtime.invalidate_scene(here, force=True)
    here.frame_set(5)
    yield 0.3
    before = dict(background.stats)
    background.delay_override = 1.0
    background.force_enabled = True
    t0 = time.time()
    while time.time() - t0 < 40 and runtime.cache_info(here)[0] < 24:
        yield 0.25
    count = runtime.cache_info(here)[0]
    check("sitting still fills the cache in the background", count == 24,
          f"{count} frames in {time.time() - t0:.1f}s, {background.stats}, {background.status(here)!r}"
          f" same runtime {runtime.peek(here) is rt2}, {dict(rt2.counts)},"
          f" {[(r.name, len(r.cache), r.cache_gen, r.bg_done, r.bg_reason) for r in rt2.rigs.values()]}"
          f" {background.last_error.strip()[-300:]}")
    check("its own work never counted as activity", background.stats["interrupted"] == before["interrupted"],
          f"{before} -> {background.stats}")
    yield 1.5
    sc = background.cache_scene()
    check("it tidies up after itself", background._session is None and sc is not None and len(sc.objects) == 0
          and not any(background.COPY_TAG in o for o in bpy.data.objects))
    rt2.counts.clear()
    worst = 0.0
    for f in range(1, 25):
        here.frame_set(f)
        worst = max(worst, mdiff(helper_mat("w2"), ref[f]))
    check("the background frames are exactly what the viewport simulates",
          worst < 1e-4 and rt2.counts.get("CACHE", 0) == 24 and not rt2.counts.get("SIM"),
          f"{worst:.2e} {dict(rt2.counts)}")

    # an edit on the rig marks it for a rebuild that normally waits for the next frame change,
    # the background cache has to get past that by itself
    root_pb = bpy.data.objects["Rig"].pose.bones["root"]
    root_pb.rotation_quaternion = (1.0, 0.08, 0.0, 0.0)
    yield 0.5
    t0 = time.time()
    while time.time() - t0 < 40 and runtime.cache_info(here)[0] < 24:
        yield 0.25
    count = runtime.cache_info(here)[0]
    check("after editing the rig it still fills the cache by itself", count == 24,
          f"{count} frames in {time.time() - t0:.1f}s, dirty {rt2.structure_dirty}, {background.stats},"
          f" {dict(rt2.counts)}")
    root_pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    yield 0.3

    # doing anything stops it right away (a long range, so there's time to catch it working)
    here.frame_end = 2000
    runtime.invalidate_scene(here, force=True)
    here.frame_set(5)
    t0 = time.time()
    while time.time() - t0 < 20 and not (background._session is not None and background._session.gen is not None):
        yield 0.05
    running = background._session is not None
    here.frame_set(7)
    yield 0.8
    check("doing something stops it right away", running and background._session is None
          and not any(background.COPY_TAG in o for o in bpy.data.objects),
          f"was running {running}, {background.stats}")
    background.force_enabled = False
    here.frame_end = 24

    # saving never writes the hidden here
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "with_background.blend"), copy=True)
    check("saving removes the hidden here", background.cache_scene() is None)

    # --- slow playback drops frames: the wiggle goes on instead of starting over every few frames
    here.frame_end = 200
    sync = here.sync_mode
    here.sync_mode = "FRAME_DROP"
    runtime.invalidate_scene(here, force=True)
    here.frame_set(1)

    calls = []

    def slow(sc, *_args):
        calls.append(sc.frame_current)
        time.sleep(1.5 if len(calls) == 3 else 0.3)  # one frame slower than a second's worth of frames
    bpy.app.handlers.frame_change_post.append(slow)
    rt2.counts.clear()
    try:
        with bpy.context.temp_override(**ctx()):
            bpy.ops.screen.animation_play()
        t0 = time.time()
        while time.time() - t0 < 3.5:
            yield 0.1
        with bpy.context.temp_override(**ctx()):
            bpy.ops.screen.animation_cancel(restore_frame=False)
    finally:
        bpy.app.handlers.frame_change_post.remove(slow)
    yield 0.3
    check("slow playback drops frames", rt2.counts.get("dropped frames", 0) >= 10, f"{dict(rt2.counts)}")
    check("and the wiggle keeps going instead of starting over", not rt2.counts.get("RESET"), f"{dict(rt2.counts)}")
    check("the playback speed shows up in the debug numbers", 200 < rt2.frame_ms < 2000, f"{rt2.frame_ms:.0f} ms")
    here.frame_end = 24
    here.sync_mode = sync

    # --- Wiggle Groups in a real window: picking a group or bones leaves the cache alone
    from EmilsWiggle import groups
    rig_ob = bpy.data.objects["Rig"]
    bpy.context.view_layer.objects.active = rig_ob
    with bpy.context.temp_override(**ctx()):
        bpy.ops.object.mode_set(mode="POSE")
    tips = groups.new_group(rig_ob, "Tips")
    for name in ("w1", "w2"):
        rig_ob.pose.bones[name].emils_wiggle.group = tips.uid
    runtime.invalidate_scene(here, force=True)
    for f in range(1, 25):
        here.frame_set(f)
    here.frame_set(12)
    yield 0.5
    rt2.counts.clear()
    for pb in rig_ob.pose.bones:
        pb.bone.select = False
    rig_ob.emils_wiggle.active_group = 0  # what a click in the list does
    yield 0.5
    picked = sorted(pb.name for pb in rig_ob.pose.bones if pb.bone.select)
    check("picking a group in a real window selects its bones", picked == ["w1", "w2"], f"{picked}")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.pose.select_all(action="SELECT")
    yield 0.5
    with bpy.context.temp_override(**ctx()):
        bpy.ops.emils_wiggle.group_select()
    yield 0.5
    count = runtime.cache_info(here)[0]
    check("and neither that nor selecting bones clears the cache", count == 24 and rt2.counts["clicks ignored"] >= 3,
          f"{count} {dict(rt2.counts)}")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.screen.animation_play()
    yield 1.0
    with bpy.context.temp_override(**ctx()):
        bpy.ops.screen.animation_cancel(restore_frame=False)
    yield 0.3
    rig_ob.pose.bones["w1"].rotation_quaternion = (1.0, 0.1, 0.0, 0.0)
    yield 0.5
    count = runtime.cache_info(here)[0]
    check("playing in pose mode works and posing a bone still clears the cache",
          count < 24 and not rt2.counts.get("errors"), f"{count} {dict(rt2.counts)}")
    rig_ob.pose.bones["w1"].rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    with bpy.context.temp_override(**ctx()):
        bpy.ops.object.mode_set(mode="OBJECT")
    yield 0.3

    # --- New Scene > Full Copy: the window switches to a scene whose depsgraph was built but never
    # evaluated, then the frame handlers run. The copied rig needs its own empties, and making them
    # in frame_change_pre crashed Blender 3.6 in the depsgraph rebuild right after.
    win = bpy.context.window_manager.windows[0]
    src = win.scene
    for f in (1, 2, 3):
        src.frame_set(f)
    yield 0.3
    before = helpers()
    src_mat = helper_mat("w2")
    with bpy.context.temp_override(**ctx()):
        bpy.ops.scene.new(type="FULL_COPY")
    yield 0.5
    copy = win.scene
    check("full scene copy doesn't crash", copy != src and copy.emils_wiggle.enabled)
    copy.frame_set(copy.frame_current + 1)
    yield 0.3
    rig_copy = next(o for o in copy.objects if o.type == "ARMATURE")
    c = runtime.find_constraint(rig_copy.pose.bones["w2"])
    check("the copied rig got its own empties", c is not None and c.target is not None
          and c.target.name not in before, f"{c.target.name if c and c.target else None}")
    check("and it never moved the original rig's empties", mdiff(helper_mat("w2"), src_mat) < 1e-6)

    # --- same thing when switching to a scene that was never shown
    other = bpy.data.scenes.new("Unseen")
    other.frame_start, other.frame_end = 1, 24
    rig2 = rig_copy.copy()  # its constraints still point at the copy's empties
    rig2.data = rig_copy.data.copy()
    rig2.parent = None
    other.collection.objects.link(rig2)
    other.emils_wiggle["enabled"] = True  # no update callback, so nothing gets set up yet
    taken = helpers()
    win.scene = other
    yield 0.5
    other.frame_set(2)
    yield 0.3
    c2 = runtime.find_constraint(rig2.pose.bones["w2"])
    check("switching to a never shown scene sets its rigs up without crashing",
          c2 is not None and c2.target is not None and c2.target.name not in taken,
          f"{c2.target.name if c2 and c2.target else None}")

    # --- Alembic export as a background job runs the frame handlers on its own thread
    win.scene = src
    yield 0.3
    rig_src = bpy.data.objects["Rig"]
    rig_src.pose.bones["root"].emils_wiggle["use_tail"] = True  # needs a new empty, no update callback
    runtime.clear_all()
    count = len(helpers())
    threads = []

    def thread_spy(sc, *args):
        # runs after our frame_change_post, so a helper made there would already be counted
        import threading
        if threading.current_thread() is not threading.main_thread():
            threads.append((sc.frame_current, len(helpers())))
    bpy.app.handlers.frame_change_post.append(thread_spy)
    errors = handlers.error_count
    with bpy.context.temp_override(**ctx()):
        bpy.ops.wm.alembic_export(filepath=os.path.join(OUT, "rig.abc"), start=1, end=6,
                                  as_background_job=True)
    yield 0.5
    t0 = time.time()
    while bpy.context.window_manager.is_interface_locked and time.time() - t0 < 60:
        yield 0.25
    bpy.app.handlers.frame_change_post.remove(thread_spy)
    check("alembic export runs our handlers off the main thread", [f for f, _n in threads][:6] == [1, 2, 3, 4, 5, 6],
          f"{threads}")
    check("and nothing gets created from that thread",
          all(n == count for _f, n in threads) and handlers.error_count == errors,
          f"{count}: {threads}, errors {handlers.error_count - errors}")
    check("the file got written", os.path.exists(os.path.join(OUT, "rig.abc")))
    src.frame_set(3)
    yield 0.3
    check("the next frame on the main thread sets the new bone up", len(helpers()) == count + 1,
          f"{count} -> {len(helpers())}")


def helpers():
    return {o.name for o in bpy.data.objects if runtime.HELPER_TAG in o}


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


bpy.app.timers.register(tick, first_interval=1.0, persistent=True)


def watchdog():
    results.append("TIMEOUT")
    finish()


bpy.app.timers.register(watchdog, first_interval=240.0, persistent=True)
