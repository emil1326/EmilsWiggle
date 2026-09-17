"""
Stress test for Emil's Wiggle: hundreds of random edits, frame changes, renders,
undos and file reloads, trying to crash Blender or break the add-on.

    blender -b --factory-startup --python tests/stress.py -- <result.txt> [seed] [ops]
    blender --factory-startup --python tests/stress.py -- <result.txt> [seed] [ops]   (GUI: adds undo,
                                                                                       playback, threaded renders)

Every action is written to <result.txt>.log before it runs, so after a crash the last
line says what did it (plus faulthandler's Python stack). tests/run_all.py --stress runs it.
"""

import faulthandler
import math
import os
import random
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
RESULT = argv[0] if argv else os.path.join(tempfile.gettempdir(), "emils_wiggle_stress.txt")
SEED = int(argv[1]) if len(argv) > 1 else 1
OPS = int(argv[2]) if len(argv) > 2 else 300
_log = open(RESULT + ".log", "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = _log
faulthandler.enable(_log, all_threads=True)

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

import EmilsWiggle  # noqa: E402
from EmilsWiggle import background, handlers, runtime  # noqa: E402

if os.environ.get("EMILS_WIGGLE_STRESS_NO_HANDLERS"):
    handlers.register = lambda: None  # repro helper: is it us or Blender?
EmilsWiggle.register()

GUI = not bpy.app.background
# the background cache kicks in between actions as often as possible
background.force_enabled = True
background.delay_override = 0.15
background.WARMUP_QUIET = 0.05
background.POLL = 0.05
if os.environ.get("EMILS_WIGGLE_STRESS_NO_SAVE_CLEANUP"):
    background.remove_all = lambda: None  # repro helper: leave the hidden scene alone when saving
rng = random.Random(SEED)
TMP = tempfile.mkdtemp(prefix="emils_wiggle_stress_")
stats = {"ops": 0, "op_errors": 0, "renders": 0, "undos": 0, "reloads": 0}
problems = []
op_errors = {}
slow = []
SLOW = 5.0  # seconds; a single action taking longer than this gets reported (not a failure)


def log(*a):
    print(*a, flush=True)


def problem(text):
    problems.append(text)
    log("PROBLEM:", text)


# ------------------------------------------------------------------ helpers

def scenes():
    return list(bpy.data.scenes)


def cur_scene():
    if GUI:
        return bpy.context.window_manager.windows[0].scene
    return bpy.context.scene


def armatures(scene=None):
    obs = (scene or cur_scene()).objects
    return [o for o in obs if o.type == "ARMATURE" and o.pose is not None]


def pick(seq):
    seq = list(seq)
    return rng.choice(seq) if seq else None


def wiggle_bones(ob):
    return [pb for pb in ob.pose.bones if pb.emils_wiggle.use_tail or pb.emils_wiggle.use_head]


def rig_summary():
    s = cur_scene()
    w = s.emils_wiggle
    rigs = armatures(s)
    bones = sum(len(wiggle_bones(o)) for o in rigs)
    return (f"{len(rigs)} rigs, {bones} wiggle bones, it {w.iterations}, sub {w.substeps}, "
            f"preroll {w.preroll}, cache {w.use_cache}, range {s.frame_start}-{s.frame_end}")


def ctx():
    win = bpy.context.window_manager.windows[0]
    area = next((a for a in win.screen.areas if a.type == "VIEW_3D"), win.screen.areas[0])
    region = next(r for r in area.regions if r.type == "WINDOW")
    return dict(window=win, screen=win.screen, area=area, region=region)


def set_active(ob):
    vl = bpy.context.view_layer
    if ob.name in vl.objects:
        vl.objects.active = ob
        return True
    return False


def ensure_object_mode():
    ob = bpy.context.object
    if ob is not None and ob.mode != "OBJECT":
        try:
            if GUI:
                with bpy.context.temp_override(**ctx()):
                    bpy.ops.object.mode_set(mode="OBJECT")
            else:
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass


def mode_set(mode):
    if GUI:
        with bpy.context.temp_override(**ctx()):
            bpy.ops.object.mode_set(mode=mode)
    else:
        bpy.ops.object.mode_set(mode=mode)


# ------------------------------------------------------------------ actions

def act_new_rig():
    scene = cur_scene()
    n = rng.randint(1, 6)
    arm = bpy.data.armatures.new("StressRig")
    ob = bpy.data.objects.new("StressRig", arm)
    scene.collection.objects.link(ob)
    ob.location = (rng.uniform(-3, 3), rng.uniform(-3, 3), 0)
    ensure_object_mode()
    set_active(ob)
    mode_set("EDIT")
    prev = arm.edit_bones.new("root")
    prev.head, prev.tail = (0, 0, 0), (0, 0, rng.uniform(0.05, 2))
    for i in range(n):
        eb = arm.edit_bones.new(f"b{i}")
        connect = rng.random() < 0.7
        eb.head = prev.tail if connect else prev.tail + Vector((rng.uniform(-.3, .3), 0, .1))
        eb.tail = eb.head + Vector((rng.uniform(-.5, .5), rng.uniform(-.5, .5), rng.uniform(.05, 1)))
        eb.parent = prev
        eb.use_connect = connect
        eb.roll = rng.uniform(-3, 3)
        if rng.random() < 0.5:
            prev = eb
    mode_set("OBJECT")
    for pb in ob.pose.bones:
        if pb.name != "root" or rng.random() < 0.3:
            pb.emils_wiggle.use_tail = rng.random() < 0.8
            pb.emils_wiggle.use_head = rng.random() < 0.3
    root = ob.pose.bones["root"]
    for f in (1, rng.randint(5, 20), rng.randint(21, 40)):
        root.location = (rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(-1, 1))
        root.rotation_quaternion = (1, rng.uniform(-.5, .5), rng.uniform(-.5, .5), 0)
        root.keyframe_insert("location", frame=f)
        root.keyframe_insert("rotation_quaternion", frame=f)
    return ob.name


def act_delete_rig():
    ob = pick(armatures())
    if ob is not None:
        bpy.data.objects.remove(ob)


def act_duplicate_rig():
    ob = pick(armatures())
    if ob is None:
        return
    copy = ob.copy()
    if rng.random() < 0.5:
        copy.data = ob.data.copy()
    cur_scene().collection.objects.link(copy)


def act_rename():
    ob = pick(armatures())
    if ob is None:
        return
    if rng.random() < 0.5:
        ob.name = f"Renamed{rng.randint(0, 99)}"
    else:
        pb = pick(ob.pose.bones)
        pb.name = f"{pb.name}_r{rng.randint(0, 9)}"


def act_toggle_bone():
    ob = pick(armatures())
    pb = pick(ob.pose.bones) if ob else None
    if pb is None:
        return
    s = pb.emils_wiggle
    what = rng.choice(["use_tail", "use_head", "mute"])
    setattr(s, what, not getattr(s, what))


def act_toggle_object():
    ob = pick(armatures())
    if ob is None:
        return
    what = rng.choice(["mute", "freeze"])
    setattr(ob.emils_wiggle, what, not getattr(ob.emils_wiggle, what))


def act_settings():
    ob = pick(armatures())
    pb = pick(ob.pose.bones) if ob else None
    if pb is None:
        return
    side = rng.choice([pb.emils_wiggle.tail, pb.emils_wiggle.head])
    extreme = rng.random() < 0.3
    side.mass = rng.choice([0.01, 1.0, 1000.0]) if extreme else rng.uniform(0.1, 5)
    side.stiff = rng.choice([0.0, 1e6, 1e12]) if extreme else rng.uniform(0, 3000)
    side.stretch = rng.random()
    side.damp = rng.choice([0.0, 1e4]) if extreme else rng.uniform(0, 30)
    side.gravity = rng.choice([-1e4, 0.0, 1e4]) if extreme else rng.uniform(-3, 3)
    side.per_axis = rng.random() < 0.4
    side.stiff_axis = [rng.choice([0.0, 400.0, 1e7]) for _ in range(3)]
    side.damp_axis = [rng.choice([0.0, 1.0, 500.0]) for _ in range(3)]
    side.gravity_axis = [rng.uniform(-10, 10) for _ in range(3)]
    side.lock = [rng.random() < 0.3 for _ in range(3)]
    side.chain = rng.random() < 0.8
    side.radius = rng.choice([0.0, 0.1, 5.0])
    side.friction = rng.uniform(0, 2)
    side.bounce = rng.uniform(0, 2)
    side.sticky = rng.uniform(0, 1)


def act_scene_settings():
    s = cur_scene().emils_wiggle
    what = rng.choice(["loop", "fast_preview", "use_cache", "cache_locked", "iterations", "substeps",
                       "preroll", "enabled", "edit_selected"])
    if what == "iterations":
        s.iterations = rng.choice([1, 2, 10, 40])
    elif what == "substeps":
        s.substeps = rng.choice([1, 2, 8])
    elif what == "preroll":
        s.preroll = rng.choice([0, 3, 60])
    else:
        setattr(s, what, not getattr(s, what))
        if what == "enabled" and not s.enabled and rng.random() < 0.7:
            s.enabled = True


def act_frames():
    scene = cur_scene()
    kind = rng.random()
    if kind < 0.5:
        start = rng.randint(-5, 70)
        for f in range(start, start + rng.randint(1, 25)):
            scene.frame_set(f)
    elif kind < 0.8:
        scene.frame_set(rng.randint(-20, 120))
    else:
        scene.frame_set(scene.frame_current, subframe=rng.random())


def act_timing():
    scene = cur_scene()
    r = scene.render
    what = rng.choice(["fps", "range", "preview"])
    if what == "fps":
        r.fps = rng.choice([1, 12, 24, 30, 60, 240])
        r.fps_base = rng.choice([1.0, 1.001, 0.1, 10.0])
    elif what == "range":
        a = rng.randint(-10, 30)
        scene.frame_start, scene.frame_end = a, a + rng.randint(0, 80)
    else:
        scene.use_preview_range = not scene.use_preview_range
        scene.frame_preview_start = rng.randint(1, 20)
        scene.frame_preview_end = scene.frame_preview_start + rng.randint(0, 30)


def act_transform_rig():
    ob = pick(armatures())
    if ob is None:
        return
    choice = rng.random()
    if choice < 0.15:
        ob.scale = (0.0, 0.0, 0.0)
    elif choice < 0.3:
        ob.scale = (rng.choice([-1, 1]) * rng.uniform(0.001, 50),) * 3
    elif choice < 0.45:
        ob.scale = (rng.uniform(0.1, 3), rng.uniform(0.1, 3), rng.uniform(0.1, 3))
    else:
        ob.rotation_euler = (rng.uniform(-7, 7), rng.uniform(-7, 7), rng.uniform(-7, 7))
    ob.location = (rng.uniform(-5, 5), rng.uniform(-5, 5), rng.uniform(-5, 5))


def act_pose_bone():
    ob = pick(armatures())
    pb = pick(ob.pose.bones) if ob else None
    if pb is None:
        return
    pb.rotation_mode = rng.choice(["QUATERNION", "XYZ", "ZXY", "AXIS_ANGLE"])
    pb.location = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
    pb.scale = (rng.choice([0.0, 0.5, 1.0, 3.0]),) * 3
    if rng.random() < 0.3:
        pb.keyframe_insert("location", frame=rng.randint(1, 60))


def act_edit_bones():
    ob = pick(armatures())
    if ob is None or not set_active(ob):
        return
    ensure_object_mode()
    mode_set("EDIT")
    ebs = ob.data.edit_bones
    kind = rng.random()
    target = pick(ebs)
    if target is not None:
        if kind < 0.3 and len(ebs) > 1:
            ebs.remove(target)
        elif kind < 0.55:
            eb = ebs.new(f"added{rng.randint(0, 999)}")
            eb.head = target.tail.copy()
            eb.tail = eb.head + Vector((0, 0, rng.uniform(0.01, 1)))
            eb.parent = target
            eb.use_connect = rng.random() < 0.5
        elif kind < 0.8:
            other = pick(ebs)
            if other is not None and other != target:
                try:
                    target.parent = other
                except Exception:
                    pass
                target.use_connect = rng.random() < 0.3
        else:
            target.use_connect = False
            target.tail = target.head + Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(0.01, 1)))
            target.inherit_scale = rng.choice(["FULL", "FIX_SHEAR", "AVERAGE", "NONE", "NONE_LEGACY", "ALIGNED"])
            target.use_inherit_rotation = rng.random() < 0.7
            target.use_local_location = rng.random() < 0.8
            target.use_relative_parent = rng.random() < 0.2
    mode_set("OBJECT")


def _add_target_object(kind):
    scene = cur_scene()
    if kind == "mesh":
        me = bpy.data.meshes.new("StressMesh")
        if rng.random() < 0.7:
            s = rng.uniform(0.2, 3)
            me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
        ob = bpy.data.objects.new("StressCollider", me)
    elif kind == "wind":
        ob = bpy.data.objects.new("StressWind", None)
        scene.collection.objects.link(ob)
        ensure_object_mode()
        set_active(ob)
        if GUI:
            with bpy.context.temp_override(**ctx(), active_object=ob, object=ob):
                bpy.ops.object.forcefield_toggle()
        else:
            with bpy.context.temp_override(active_object=ob, object=ob):
                bpy.ops.object.forcefield_toggle()
        ob.field.type = "WIND"
        ob.field.strength = rng.uniform(-50, 50)
        ob.rotation_euler = (rng.uniform(-3, 3), 0, rng.uniform(-3, 3))
        return ob
    else:
        ob = bpy.data.objects.new("StressEmpty", None)
    ob.location = (rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-2, 2))
    scene.collection.objects.link(ob)
    return ob


def act_collider_wind_pin():
    ob = pick(armatures())
    pb = pick(ob.pose.bones) if ob else None
    if pb is None:
        return
    side = pb.emils_wiggle.tail
    kind = rng.random()
    if kind < 0.3:
        side.collider_type = "Object"
        side.collider = _add_target_object("mesh")
    elif kind < 0.5:
        coll = pick(bpy.data.collections) or bpy.data.collections.new("StressColl")
        if coll.name not in cur_scene().collection.children and coll != runtime.helper_collection(False):
            try:
                cur_scene().collection.children.link(coll)
            except RuntimeError:
                pass
        if coll != runtime.helper_collection(False):
            coll.objects.link(_add_target_object("mesh")) if rng.random() < 0.8 else None
            side.collider_type = "Collection"
            side.collider_collection = coll
    elif kind < 0.7:
        side.wind_ob = _add_target_object("wind")
        side.wind = rng.uniform(-5, 5)
    else:
        c = pb.constraints.new(rng.choice(["DAMPED_TRACK", "TRACK_TO", "LOCKED_TRACK", "COPY_ROTATION",
                                            "LIMIT_ROTATION", "IK"]))
        if hasattr(c, "target"):
            c.target = _add_target_object("empty")
        c.influence = rng.random()


def act_delete_thing():
    kind = rng.random()
    names = [o.name for o in bpy.data.objects if o.name.startswith(("StressCollider", "StressWind", "StressEmpty"))]
    if kind < 0.5 and names:
        bpy.data.objects.remove(bpy.data.objects[pick(names)])
    elif kind < 0.65:
        coll = pick([c for c in bpy.data.collections if c.name.startswith("StressColl")])
        if coll is not None:
            bpy.data.collections.remove(coll)
    elif kind < 0.8:
        helper = pick([o for o in bpy.data.objects if runtime.HELPER_TAG in o])
        if helper is not None:
            bpy.data.objects.remove(helper)  # someone deletes one of our empties
    elif kind < 0.85:
        coll = runtime.helper_collection(False)
        if coll is not None:
            bpy.data.collections.remove(coll)  # or the whole helper collection
    else:
        ob = pick(armatures())
        pb = pick(ob.pose.bones) if ob else None
        c = runtime.find_constraint(pb) if pb is not None else None
        if c is not None:
            r = rng.random()
            if r < 0.4:
                pb.constraints.remove(c)
            elif r < 0.7:
                c.mute = True
                c.influence = rng.random()
            else:
                other = pb.constraints.new("COPY_LOCATION")
                pb.constraints.move(len(pb.constraints) - 1, 0)
                other.influence = 0.0


def act_operators():
    scene = cur_scene()
    op = rng.choice(["reset", "simulate", "clear_cache", "select", "copy", "bake", "import"])
    ob = pick(armatures())
    if op == "reset":
        bpy.ops.emils_wiggle.reset()
    elif op == "simulate":
        scene.frame_end = scene.frame_start + rng.randint(0, 15)
        bpy.ops.emils_wiggle.simulate(lock=rng.random() < 0.5)
    elif op == "clear_cache":
        bpy.ops.emils_wiggle.clear_cache()
    elif ob is not None and set_active(ob):
        ensure_object_mode()
        mode_set("POSE")
        for pb in ob.pose.bones:
            pb.bone.select = rng.random() < 0.5
        ob.data.bones.active = pick(ob.data.bones)
        try:
            if op == "select":
                bpy.ops.emils_wiggle.select()
            elif op == "copy":
                bpy.ops.emils_wiggle.copy()
            elif op == "bake":
                scene.frame_end = scene.frame_start + rng.randint(0, 6)
                bpy.ops.emils_wiggle.bake()
            elif op == "import":
                for pb in ob.pose.bones:
                    pb["wiggle_tail"] = rng.random() < 0.5
                    pb["wiggle_stiff"] = rng.uniform(0, 500)
                bpy.ops.emils_wiggle.import_wiggle2("EXEC_DEFAULT")
        finally:
            ensure_object_mode()


def act_view_layers():
    scene = cur_scene()
    kind = rng.random()
    if kind < 0.4:
        scene.view_layers.new(f"Layer{rng.randint(0, 99)}")
    elif kind < 0.7 and len(scene.view_layers) > 1:
        vl = pick(scene.view_layers)
        if not GUI or vl != bpy.context.window_manager.windows[0].view_layer:
            scene.view_layers.remove(vl)
    else:
        for vl in scene.view_layers:
            for lc in vl.layer_collection.children:
                lc.exclude = rng.random() < 0.2


def act_scenes():
    kind = rng.random()
    if kind < 0.5:
        scene = cur_scene()
        if GUI:
            kind = rng.choice(["FULL_COPY", "LINK_COPY", "NEW"])
            log("   scene.new", kind)
            with bpy.context.temp_override(**ctx()):
                bpy.ops.scene.new(type=kind)
        else:
            new = scene.copy()
            new.name = "StressCopy"
    elif len(scenes()) > 1:
        target = pick(scenes())
        if GUI and rng.random() < 0.5:
            if background.SCENE_TAG not in target:  # nobody switches to the hidden scene by hand
                bpy.context.window_manager.windows[0].scene = target
        elif target != cur_scene():
            bpy.data.scenes.remove(target)


def act_render():
    scene = cur_scene()
    if scene.camera is None:
        cam = bpy.data.objects.new("StressCam", bpy.data.cameras.new("StressCam"))
        scene.collection.objects.link(cam)
        cam.location = (0, -15, 2)
        cam.rotation_euler = (1.5708, 0, 0)
        scene.camera = cam
    r = scene.render
    r.engine = rng.choice(["BLENDER_WORKBENCH", "CYCLES"])
    if r.engine == "CYCLES":
        scene.cycles.samples = 1
        scene.cycles.device = "CPU"
        r.use_motion_blur = rng.random() < 0.5
    r.resolution_x = r.resolution_y = 16
    r.resolution_percentage = 100
    r.filepath = os.path.join(TMP, "r_")
    scene.frame_end = scene.frame_start + rng.randint(0, 4)
    others = [sc for sc in scenes() if sc != scene]
    if others and rng.random() < 0.3:
        # the compositor renders another scene too, its frame handlers run on the render thread
        other = pick(others)
        other.render.engine = "BLENDER_WORKBENCH"
        other.render.resolution_x = other.render.resolution_y = 16
        scene.use_nodes = True
        tree = scene.node_tree
        node = tree.nodes.new("CompositorNodeRLayers")
        node.scene = other
        comp = next((n for n in tree.nodes if n.type == "COMPOSITE"), None)
        if comp is None:
            comp = tree.nodes.new("CompositorNodeComposite")
        tree.links.new(node.outputs[0], comp.inputs[0])
    anim = rng.random() < 0.6
    stats["renders"] += 1
    if GUI:
        with bpy.context.temp_override(**ctx()):
            bpy.ops.render.render("INVOKE_DEFAULT", animation=anim, write_still=not anim)
        return "wait_render"
    bpy.ops.render.render(animation=anim, write_still=not anim)


def act_viewport_render():
    if not GUI:
        return
    scene = cur_scene()
    scene.render.filepath = os.path.join(TMP, "gl_")
    scene.frame_end = scene.frame_start + rng.randint(0, 3)
    with bpy.context.temp_override(**ctx()):
        bpy.ops.render.opengl(animation=rng.random() < 0.5)


def act_export():
    scene = cur_scene()
    scene.frame_end = scene.frame_start + rng.randint(0, 4)
    background = GUI and rng.random() < 0.7
    kind = rng.choice(["abc", "usd"])
    path = os.path.join(TMP, f"export_{rng.randint(0, 2)}.{kind}")
    if kind == "abc":
        op, args = bpy.ops.wm.alembic_export, dict(filepath=path, start=scene.frame_start, end=scene.frame_end)
    else:
        op, args = bpy.ops.wm.usd_export, dict(filepath=path, export_animation=True)
    log("   export", kind, "background" if background else "foreground")
    try:
        if GUI:
            with bpy.context.temp_override(**ctx()):
                op(**args, as_background_job=background)
        else:
            op(**args, as_background_job=False)
    except TypeError:  # no as_background_job on this exporter
        if GUI:
            with bpy.context.temp_override(**ctx()):
                op(**args)
        else:
            op(**args)
    return "wait_render" if background else None


def act_parent_rig():
    rigs = armatures()
    if len(rigs) < 2:
        return
    child, parent = rng.sample(rigs, 2)
    if rng.random() < 0.3:
        child.parent = None
        return
    child.parent = parent
    if child.parent == parent and rng.random() < 0.6 and len(parent.data.bones):
        child.parent_type = "BONE"
        child.parent_bone = pick(parent.data.bones).name


def act_library():
    """Link wiggle rigs from another file, sometimes as library overrides."""
    path = os.path.join(TMP, f"lib_{rng.randint(0, 1)}.blend")
    if not os.path.exists(path) or rng.random() < 0.3:
        ensure_object_mode()
        if GUI:
            with bpy.context.temp_override(**ctx()):
                bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        else:
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
    with bpy.data.libraries.load(path, link=True) as (src, dst):
        dst.objects = [n for n in src.objects if n.startswith(("StressRig", "Renamed"))][:2]
    for ob in dst.objects:
        if ob is None:
            continue
        if cur_scene().collection.objects.get(ob.name) != ob:
            try:
                cur_scene().collection.objects.link(ob)
            except RuntimeError:
                continue
        if rng.random() < 0.5:
            ob.override_create(remap_local_usages=True)


def act_background():
    """Headless: a background cache session, sometimes cut short like when the user comes back."""
    if GUI:
        return
    background.run_blocking(cur_scene(), limit=rng.choice([None, 1, 5, 40]))


def act_sit_still():
    """GUI: don't touch anything for a moment, the background cache starts (and gets interrupted)."""
    if GUI:
        return "idle"


def act_view_layer_switch():
    if not GUI:
        return
    win = bpy.context.window_manager.windows[0]
    win.view_layer = pick(win.scene.view_layers)


def act_undo():
    if not GUI:
        return
    stats["undos"] += 1
    with bpy.context.temp_override(**ctx()):
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.7:
                bpy.ops.ed.undo()
            else:
                bpy.ops.ed.redo()


def act_undo_push():
    if GUI:
        with bpy.context.temp_override(**ctx()):
            bpy.ops.ed.undo_push(message="stress")


def act_playback():
    if not GUI:
        return
    with bpy.context.temp_override(**ctx()):
        if runtime._is_playing():
            bpy.ops.screen.animation_cancel(restore_frame=rng.random() < 0.5)
        else:
            bpy.ops.screen.animation_play(reverse=rng.random() < 0.2)


def act_reload():
    stats["reloads"] += 1
    path = os.path.join(TMP, f"reload_{rng.randint(0, 3)}.blend")
    ensure_object_mode()
    home = rng.random() < 0.15  # File > New
    if GUI:
        with bpy.context.temp_override(**ctx()):
            if runtime._is_playing() and rng.random() < 0.5:
                bpy.ops.screen.animation_cancel()  # otherwise it loads while playing
            if home:
                bpy.ops.wm.read_homefile(use_empty=True)
            else:
                bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
                bpy.ops.wm.open_mainfile(filepath=path)
    elif home:
        bpy.ops.wm.read_homefile(use_empty=True)
    else:
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        bpy.ops.wm.open_mainfile(filepath=path)
    if home:
        cur_scene().emils_wiggle.enabled = True


def act_addon_toggle():
    EmilsWiggle.unregister()
    if rng.random() < 0.3:
        cur_scene().frame_set(cur_scene().frame_current + 1)  # frames while we're off
    EmilsWiggle.register()


def act_wiggle2_leftovers():
    """Fake what disabling Wiggle 2 at runtime leaves behind, then keep going."""
    names = {"Scene": "WiggleScene", "Object": "WiggleObject", "PoseBone": "WiggleBone"}
    for owner, type_name in names.items():
        if hasattr(bpy.types, type_name):
            continue
        cls = type(type_name, (bpy.types.PropertyGroup,), {"__annotations__": {"x": bpy.props.FloatProperty()}})
        bpy.utils.register_class(cls)
        setattr(getattr(bpy.types, owner), "wiggle", bpy.props.PointerProperty(type=cls))
        if owner == "Scene":
            cur_scene().wiggle.x = 1.0
        bpy.utils.unregister_class(cls)
    if not GUI:
        # A real Wiggle 2 gets cleaned up the moment it's turned off (legacy.hook_wiggle2), and the
        # GUI has the timer for anything else. Background Blender runs no timers, so do it here.
        from EmilsWiggle import legacy
        legacy.clean_dangling_wiggle2()


def _group_op(name):
    op = getattr(bpy.ops.emils_wiggle, name)
    try:
        if GUI:
            with bpy.context.temp_override(**ctx()):
                if op.poll():
                    op()
        elif op.poll():
            op()
    except Exception:
        problem(f"Wiggle Group operator {name} failed: {traceback.format_exc()[-300:]}")


def act_groups():
    from EmilsWiggle import groups
    ob = pick(armatures())
    if ob is None or not set_active(ob):
        return
    ensure_object_mode()
    mode_set("POSE")
    try:
        s = ob.emils_wiggle
        for pb in ob.pose.bones:
            if rng.random() < 0.3:
                pb.bone.select = not pb.bone.select
            if rng.random() < 0.05:
                pb.bone.hide = not pb.bone.hide
        if len(ob.data.bones) and rng.random() < 0.5:
            ob.data.bones.active = pick(ob.data.bones)
        kind = rng.random()
        if kind < 0.2 or not s.groups:
            _group_op("group_add")
        elif kind < 0.3:
            _group_op("group_remove")
        elif kind < 0.45:
            _group_op("group_assign")
        elif kind < 0.55:
            _group_op("group_unassign")
        elif kind < 0.7 and runtime._editable(ob):
            index = rng.randrange(len(s.groups) + 2)  # past the end too
            s.active_group = index
            g = groups.active(ob)
            if g is not None:
                want = sorted(pb.name for pb in groups.members(ob, g.uid) if groups.visible(pb))
                got = sorted(pb.name for pb in ob.pose.bones if pb.bone.select and groups.visible(pb))
                if want != got:
                    problem(f"picking group {g.name} selected {got}, expected {want}")
        elif kind < 0.8 and runtime._editable(ob):
            pick(s.groups).name = rng.choice(["", "Hair", "Hair", "x" * 70, "Ünïcödé"])
        elif kind < 0.9:
            _group_op(rng.choice(["group_select", "group_deselect"]))
        elif runtime._editable(ob) and len(ob.pose.bones):
            pick(ob.pose.bones).emils_wiggle.group = rng.randint(0, 50)  # groups that may not exist
    finally:
        ensure_object_mode()


def act_mode_switch():
    ob = pick(armatures())
    if ob is None or not set_active(ob):
        return
    ensure_object_mode()
    mode_set(rng.choice(["POSE", "EDIT", "OBJECT"]))
    cur_scene().frame_set(cur_scene().frame_current + 1)
    ensure_object_mode()


ACTIONS = [
    (act_new_rig, 6), (act_delete_rig, 2), (act_duplicate_rig, 3), (act_rename, 3),
    (act_toggle_bone, 6), (act_toggle_object, 3), (act_settings, 8), (act_scene_settings, 5),
    (act_frames, 25), (act_timing, 3), (act_transform_rig, 4), (act_pose_bone, 4),
    (act_edit_bones, 5), (act_collider_wind_pin, 5), (act_delete_thing, 5), (act_operators, 4),
    (act_view_layers, 2), (act_scenes, 2), (act_render, 3), (act_reload, 1), (act_addon_toggle, 1),
    (act_wiggle2_leftovers, 1), (act_mode_switch, 3), (act_groups, 5), (act_export, 1), (act_parent_rig, 2),
    (act_library, 1), (act_viewport_render, 1 if GUI else 0), (act_view_layer_switch, 1 if GUI else 0),
    (act_background, 0 if GUI else 3), (act_sit_still, 6 if GUI else 0),
    (act_undo, 6 if GUI else 0), (act_undo_push, 4 if GUI else 0), (act_playback, 6 if GUI else 0),
]
WEIGHTS = [w for _a, w in ACTIONS]


# ------------------------------------------------------------------ invariants

def check_invariants(label):
    for scene in scenes():
        for ob in scene.objects:
            if runtime.HELPER_TAG in ob:
                problem(f"{label}: helper {ob.name} ended up in scene {scene.name}")
    hidden = background.cache_scene()
    if GUI and hidden is not None and any(w.scene == hidden for w in bpy.context.window_manager.windows):
        problem(f"{label}: the background cache's scene is shown in a window")
    for ob in bpy.data.objects:
        if background.COPY_TAG in ob and any(sc != hidden for sc in ob.users_scene):
            problem(f"{label}: background copy {ob.name} is in {[s.name for s in ob.users_scene]}")
        if runtime.HELPER_TAG in ob:
            vals = list(ob.location) + list(ob.rotation_quaternion) + list(ob.scale)
            if not all(math.isfinite(v) for v in vals):
                problem(f"{label}: helper {ob.name} has non-finite transform {vals}")
    if handlers.error_count:
        problem(f"{label}: {handlers.error_count} handler error(s): {runtime.last_error.strip()[-400:]}")
        handlers.error_count = 0
    for scene in scenes():
        rt = runtime.peek(scene)
        if rt is not None and rt.counts.get("errors"):
            problem(f"{label}: {rt.counts['errors']} rig error(s) in {scene.name}: {runtime.last_error.strip()[-400:]}")
            rt.counts["errors"] = 0


def setup():
    scene = cur_scene()
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    scene.emils_wiggle.enabled = True
    scene.frame_start, scene.frame_end = 1, 40
    for _ in range(2):
        act_new_rig()


DUMP_AT = int(os.environ.get("EMILS_WIGGLE_STRESS_DUMP", "0"))  # save the file before this action


def run_one(i):
    action = rng.choices(ACTIONS, weights=WEIGHTS)[0][0]
    name = action.__name__
    if i == DUMP_AT:
        path = f"{RESULT}.step{i}.blend"
        if GUI:
            with bpy.context.temp_override(**ctx()):
                bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        else:
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        log("saved", path)
    log(f"[{i}] {name} (scene {cur_scene().name}, frame {cur_scene().frame_current})")
    stats["ops"] += 1
    result = None
    started = time.perf_counter()
    try:
        result = action()
    except Exception as e:
        stats["op_errors"] += 1
        key = f"{name}: {type(e).__name__}: {str(e)[:120]}"
        op_errors[key] = op_errors.get(key, 0) + 1
        log("   op error:", key)
        ensure_object_mode()
    took = time.perf_counter() - started
    if took > SLOW:
        slow.append(f"[{i}] {name} took {took:.1f}s ({rig_summary()})")
        log("   SLOW:", slow[-1])
    try:
        check_invariants(f"after [{i}] {name}")
    except Exception:
        problem(f"invariant check crashed after [{i}] {name}: {traceback.format_exc()[-300:]}")
    return result


def finish():
    lines = [f"Blender {bpy.app.version_string} | {'GUI' if GUI else 'background'} | seed {SEED}",
             f"stats: {stats}", f"background cache: {background.stats}"]
    if background.last_error:
        problems.append("background cache error: " + background.last_error.strip()[-400:])
    for text in problems:
        lines.append("FAIL  " + text)
    lines.append(f"PASS  survived {stats['ops']} random actions" if not problems
                 else f"FAIL  {len(problems)} problem(s) in {stats['ops']} actions")
    for text in slow[:10]:
        lines.append("SLOW  " + text)
    lines.append("op errors (fine, just actions that didn't apply):")
    for key, n in sorted(op_errors.items(), key=lambda kv: -kv[1])[:15]:
        lines.append(f"  {n:3d}x {key}")
    with open(RESULT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os._exit(0)


if GUI:
    bpy.context.preferences.view.render_display_type = "NONE"
    state = {"i": 0, "setup": False}

    def tick():
        try:
            if not state["setup"]:
                setup()
                state["setup"] = True
                return 0.2
            if bpy.app.is_job_running("RENDER") or bpy.context.window_manager.is_interface_locked:
                return 0.2  # renders and exports lock the interface, data mustn't change meanwhile
            if state["i"] >= OPS:
                if runtime._is_playing():
                    with bpy.context.temp_override(**ctx()):
                        bpy.ops.screen.animation_cancel()
                finish()
            state["i"] += 1
            result = run_one(state["i"])
            if result == "wait_render":
                return 0.5
            if result == "idle":
                return rng.uniform(0.2, 2.5)
            return 0.02 if rng.random() < 0.8 else 0.3  # sometimes let playback/redraws happen
        except Exception:
            problem("stress driver crashed: " + traceback.format_exc()[-500:])
            finish()
        return None

    bpy.app.timers.register(tick, first_interval=1.0, persistent=True)
    bpy.app.timers.register(lambda: (problems.append("TIMEOUT"), finish()), first_interval=900.0, persistent=True)
else:
    try:
        setup()
        for i in range(1, OPS + 1):
            run_one(i)
    except Exception:
        problem("stress driver crashed: " + traceback.format_exc()[-500:])
    finish()
