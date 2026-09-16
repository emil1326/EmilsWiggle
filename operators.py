"""
Emil's Wiggle - operators.
"""

import time

import bpy
from bpy.props import BoolProperty

from . import props, runtime


def _wiggle_rig(context):
    ob = context.object
    if ob is None or ob.type != "ARMATURE":
        return None
    rt = runtime.peek(context.scene)
    if rt is None:
        return None
    rig = rt.rigs.get(ob.name)
    if rig is None or rig.ptr != ob.as_pointer():
        return None
    return rig


def _copy_group(src, dst):
    for p in src.bl_rna.properties:
        pid = p.identifier
        if pid in {"rna_type", "name"}:
            continue
        value = getattr(src, pid)
        if isinstance(value, bpy.types.PropertyGroup):
            _copy_group(value, getattr(dst, pid))
            continue
        if p.is_readonly:
            continue
        if getattr(p, "is_array", False):
            value = tuple(value)
        setattr(dst, pid, value)


class EMILSWIGGLE_OT_reset(bpy.types.Operator):
    """Throw away the simulation and cache, bones start again from their rest pose"""
    bl_idname = "emils_wiggle.reset"
    bl_label = "Reset Physics"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene.emils_wiggle.enabled

    def execute(self, context):
        scene = context.scene
        runtime.reset_scene(scene)
        scene.frame_set(scene.frame_current)
        return {"FINISHED"}


class EMILSWIGGLE_OT_copy(bpy.types.Operator):
    """Copy the active bone's wiggle settings to the other selected bones"""
    bl_idname = "emils_wiggle.copy"
    bl_label = "Copy Settings to Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (context.mode == "POSE" and context.active_pose_bone is not None
                and len(context.selected_pose_bones or ()) > 1)

    def execute(self, context):
        src = context.active_pose_bone.emils_wiggle
        count = 0
        with props.batch():
            for pb in context.selected_pose_bones:
                if pb == context.active_pose_bone:
                    continue
                _copy_group(src, pb.emils_wiggle)
                count += 1
        runtime.settings_changed(context.scene, structure=True)
        self.report({"INFO"}, f"Copied wiggle settings to {count} bone(s)")
        return {"FINISHED"}


class EMILSWIGGLE_OT_select(bpy.types.Operator):
    """Select the wiggling bones of the armatures in pose mode"""
    bl_idname = "emils_wiggle.select"
    bl_label = "Select Wiggle Bones"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "POSE"

    def execute(self, context):
        bpy.ops.pose.select_all(action="DESELECT")
        for ob in context.objects_in_mode or (context.object,):
            if ob is None or ob.type != "ARMATURE":
                continue
            for pb in ob.pose.bones:
                s = pb.emils_wiggle
                if not s.mute and (s.use_tail or (s.use_head and not pb.bone.use_connect)):
                    pb.bone.select = True
        return {"FINISHED"}


class EMILSWIGGLE_OT_simulate(bpy.types.Operator):
    """Simulate the whole playback range into the cache. Renders then use exactly this result, no keyframes needed"""
    bl_idname = "emils_wiggle.simulate"
    bl_label = "Simulate Range"
    bl_options = {"REGISTER"}

    lock: BoolProperty(name="Lock Cache", description="Lock the cache once it's done", default=True)

    @classmethod
    def poll(cls, context):
        return context.scene.emils_wiggle.enabled and not bpy.app.is_job_running("RENDER")

    def execute(self, context):
        scene = context.scene
        s = scene.emils_wiggle
        frame_back = scene.frame_current
        s.cache_locked = False
        runtime.reset_scene(scene)
        rt = runtime.get(scene)
        if not rt.rigs:
            self.report({"WARNING"}, "No wiggle bones in this scene")
            return {"CANCELLED"}

        start, end = runtime.playback_range(scene)
        n = end - start + 1
        frames = []
        if s.loop and s.preroll and n > 0:
            first = (n - s.preroll % n) % n
            frames = [start + (first + i) % n for i in range(s.preroll)]
        frames.extend(range(start, end + 1))

        wm = context.window_manager
        wm.progress_begin(0, len(frames))
        t0 = time.perf_counter()
        rt.skip_static_preroll = bool(s.loop and s.preroll)
        rt.force_fast = True  # the cache is exact either way, this just skips an evaluation
        try:
            for i, f in enumerate(frames):
                scene.frame_set(f)
                wm.progress_update(i)
        finally:
            rt.skip_static_preroll = False
            rt.force_fast = False
            wm.progress_end()

        s.cache_locked = self.lock
        scene.frame_set(frame_back)
        self.report({"INFO"}, f"Simulated {len(frames)} frames in {time.perf_counter() - t0:.1f}s")
        return {"FINISHED"}


class EMILSWIGGLE_OT_clear_cache(bpy.types.Operator):
    """Forget every cached frame (the simulation keeps going from where it is)"""
    bl_idname = "emils_wiggle.clear_cache"
    bl_label = "Clear Cache"
    bl_options = {"REGISTER"}

    def execute(self, context):
        runtime.invalidate_scene(context.scene, force=True)
        return {"FINISHED"}


class EMILSWIGGLE_OT_lock_interface(bpy.types.Operator):
    """Turn on Render > Lock Interface, needed so wiggle can safely run while rendering"""
    bl_idname = "emils_wiggle.lock_interface"
    bl_label = "Enable Lock Interface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.render.use_lock_interface = True
        return {"FINISHED"}


class EMILSWIGGLE_OT_bake(bpy.types.Operator):
    """Bake this armature's wiggle bones to keyframes (optional, renders work without it)"""
    bl_idname = "emils_wiggle.bake"
    bl_label = "Bake to Keyframes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "POSE" and _wiggle_rig(context) is not None

    def execute(self, context):
        scene = context.scene
        s = scene.emils_wiggle
        ob = context.object
        ad = ob.animation_data
        if s.bake_nla and not s.bake_overwrite and ad is not None and ad.action is not None:
            track = ad.nla_tracks.new()
            track.name = ad.action.name
            track.strips.new(ad.action.name, int(ad.action.frame_range[0]), ad.action)

        bpy.ops.emils_wiggle.simulate(lock=True)
        rig = _wiggle_rig(context)
        if rig is None:
            return {"CANCELLED"}
        bpy.ops.pose.select_all(action="DESELECT")
        for b in rig.bones:
            ob.pose.bones[b.name].bone.select = True

        start, end = runtime.playback_range(scene)
        bpy.ops.nla.bake(frame_start=start, frame_end=end, only_selected=True,
                         visual_keying=True, use_current_action=s.bake_overwrite,
                         bake_types={"POSE"})
        ob.emils_wiggle.freeze = True
        if not s.bake_overwrite and ob.animation_data and ob.animation_data.action:
            ob.animation_data.action.name = "EmilsWiggleAction"
        return {"FINISHED"}


# Wiggle 2 stored its settings as plain pose bone properties. They're still in
# the file even when Wiggle 2 isn't installed.
_LEGACY_SIDE = ("mass", "stiff", "stretch", "damp", "gravity", "wind_ob", "wind",
                "collider_type", "collider", "collider_collection",
                "radius", "friction", "bounce", "sticky", "chain")


def _legacy_get(owner, key):
    try:
        return owner.get(key)
    except Exception:
        return None


class EMILSWIGGLE_OT_import_wiggle2(bpy.types.Operator):
    """Convert Wiggle 2 settings found in this scene into Emil's Wiggle settings"""
    bl_idname = "emils_wiggle.import_wiggle2"
    bl_label = "Import Wiggle 2 Settings"
    bl_options = {"REGISTER", "UNDO"}

    reset_poses: BoolProperty(
        name="Reset Wiggle Bone Poses",
        description="Wiggle 2 left its last wiggle on the bones, clear loc/rot/scale of "
                    "un-animated wiggle bones like Wiggle 2 did every frame",
        default=True)
    disable_old: BoolProperty(
        name="Turn Off Wiggle 2",
        description="Switch Wiggle 2 off on this scene so both don't simulate at once",
        default=True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        bones_done = 0
        with props.batch():
            old_scene = _legacy_get(scene, "wiggle")
            if old_scene is not None:
                for key in ("iterations", "loop", "preroll", "bake_overwrite", "bake_nla"):
                    v = old_scene.get(key)
                    if v is not None:
                        setattr(scene.emils_wiggle, key, type(getattr(scene.emils_wiggle, key))(v))
            for ob in scene.objects:
                if ob.type != "ARMATURE" or ob.pose is None:
                    continue
                for key, attr in (("wiggle_mute", "mute"), ("wiggle_freeze", "freeze")):
                    v = _legacy_get(ob, key)
                    if v is not None:
                        setattr(ob.emils_wiggle, attr, bool(v))
                for pb in ob.pose.bones:
                    tail = _legacy_get(pb, "wiggle_tail")
                    head = _legacy_get(pb, "wiggle_head")
                    if not tail and not head:
                        continue
                    self._import_bone(pb)
                    bones_done += 1
                    if self.reset_poses and not self._is_animated(ob, pb):
                        pb.location = (0, 0, 0)
                        pb.rotation_quaternion = (1, 0, 0, 0)
                        pb.rotation_euler = (0, 0, 0)
                        pb.rotation_axis_angle = (0, 0, 1, 0)
                        pb.scale = (1, 1, 1)
            if _legacy_get(scene, "wiggle_enable") and bones_done:
                scene.emils_wiggle.enabled = True
                if self.disable_old:
                    scene["wiggle_enable"] = False
        if scene.emils_wiggle.enabled:
            scene.render.use_lock_interface = True
            runtime.reset_scene(scene)
        rt = runtime.get(scene)
        rt.legacy_found = False
        self.report({"INFO"}, f"Imported Wiggle 2 settings from {bones_done} bone(s)")
        return {"FINISHED"}

    @staticmethod
    def _is_animated(ob, pb):
        ad = ob.animation_data
        if ad is None:
            return False
        prefix = 'pose.bones["%s"].' % bpy.utils.escape_identifier(pb.name)
        sources = [ad.drivers]
        if ad.action is not None:
            sources.append(ad.action.fcurves)
        return any(fc.data_path.startswith(prefix) for src in sources for fc in src)

    @staticmethod
    def _import_bone(pb):
        s = pb.emils_wiggle
        s.mute = bool(_legacy_get(pb, "wiggle_mute") or False)
        s.use_tail = bool(_legacy_get(pb, "wiggle_tail") or False)
        s.use_head = bool(_legacy_get(pb, "wiggle_head") or False)
        for side, suffix in ((s.tail, ""), (s.head, "_head")):
            for name in _LEGACY_SIDE:
                v = _legacy_get(pb, "wiggle_" + name + suffix)
                if v is None:
                    continue
                if name == "collider_type":
                    v = "Collection" if v in (1, "Collection") else "Object"
                elif name in ("wind_ob", "collider"):
                    if not isinstance(v, bpy.types.Object):
                        continue
                elif name == "collider_collection":
                    if not isinstance(v, bpy.types.Collection):
                        continue
                elif name == "chain":
                    v = bool(v)
                else:
                    v = float(v)
                try:
                    setattr(side, name, v)
                except (TypeError, ValueError):
                    pass


classes = (
    EMILSWIGGLE_OT_reset,
    EMILSWIGGLE_OT_copy,
    EMILSWIGGLE_OT_select,
    EMILSWIGGLE_OT_simulate,
    EMILSWIGGLE_OT_clear_cache,
    EMILSWIGGLE_OT_lock_interface,
    EMILSWIGGLE_OT_bake,
    EMILSWIGGLE_OT_import_wiggle2,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
