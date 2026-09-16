"""
Emil's Wiggle - settings (saved in the .blend, library overridable).

Simulation state is NOT stored here. It lives in plain Python (see runtime.py),
because every write to a bpy.props property tags the depsgraph and redraws the
whole window, which is what made Wiggle 2 slow.
"""

import bpy
from bpy.props import (
    BoolProperty,
    BoolVectorProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
)

from . import runtime

OVR = {"LIBRARY_OVERRIDABLE"}

_syncing = False
_batch = False


class batch:
    """Set lots of settings without syncing to selected bones or rebuilding each time."""

    def __enter__(self):
        global _batch
        self._prev = _batch
        _batch = True

    def __exit__(self, *exc):
        global _batch
        _batch = self._prev
        return False


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str, bpy.types.ID)):
        return value
    return tuple(value)


def _find_side(group, bones):
    """Which part of a bone's settings `group` is: '', 'tail', 'head' (None if not found)."""
    for pb in bones:
        if pb is None:
            continue
        s = pb.emils_wiggle
        if s == group:
            return ""
        if s.tail == group:
            return "tail"
        if s.head == group:
            return "head"
    return None


def _sync_to_selected(self, context, prop):
    """Copy an edited value to every selected pose bone (Wiggle 2 behaviour)."""
    global _syncing
    if _syncing or _batch or context is None:
        return
    scene = getattr(context, "scene", None)
    if scene is None or not scene.emils_wiggle.edit_selected:
        return
    bones = getattr(context, "selected_pose_bones", None)
    if not bones:
        return
    side = _find_side(self, [getattr(context, "active_pose_bone", None), *bones])
    if side is None:
        return
    value = _plain(getattr(self, prop))
    _syncing = True
    try:
        for pb in bones:
            target = pb.emils_wiggle
            if side:
                target = getattr(target, side)
            if target == self:
                continue
            if _plain(getattr(target, prop)) != value:
                setattr(target, prop, value)
    finally:
        _syncing = False


def _settings_changed(context, structure=False):
    if _batch:
        return
    scene = getattr(context, "scene", None) if context else None
    if scene is None:
        return
    runtime.settings_changed(scene, structure=structure)


def _upd(prop, structure=False):
    def update(self, context):
        if _syncing:
            return  # the edit that started the sync refreshes everything once
        _sync_to_selected(self, context, prop)
        _settings_changed(context, structure)
    return update


def _upd_per_axis(prop):
    """Turning per-axis on seeds the XYZ values from the single values."""
    def update(self, context):
        if _syncing:
            return
        if getattr(self, prop) and not _batch:
            self.stiff_axis = (self.stiff,) * 3
            self.damp_axis = (self.damp,) * 3
            self.gravity_axis = (self.gravity,) * 3
        _sync_to_selected(self, context, prop)
        _settings_changed(context)
    return update


def collider_poll(self, obj):
    return obj.type == "MESH"


def wind_poll(self, obj):
    return obj.field is not None and obj.field.type == "WIND"


class EmilsWiggleSideSettings(bpy.types.PropertyGroup):
    """Physics for one end of a bone (tail or head)."""

    mass: FloatProperty(name="Mass", description="Mass of this end, heavier ends pull their chain more",
                        min=0.01, default=1.0, override=OVR, update=_upd("mass"))
    stiff: FloatProperty(name="Stiff", description="Spring stiffness, can be large numbers",
                         min=0.0, default=400.0, override=OVR, update=_upd("stiff"))
    stretch: FloatProperty(name="Stretch", description="Stretchiness, 0 to 1",
                           min=0.0, max=1.0, default=0.0, override=OVR, update=_upd("stretch"))
    damp: FloatProperty(name="Damp", description="Damping, can be greater than 1",
                        min=0.0, default=1.0, override=OVR, update=_upd("damp"))
    gravity: FloatProperty(name="Gravity", description="Multiplier for the scene gravity",
                           default=1.0, override=OVR, update=_upd("gravity"))

    per_axis: BoolProperty(
        name="Per Axis",
        description="Set stiffness, damping and gravity separately on the bone's local X, Y and Z axes",
        default=False, override=OVR, update=_upd_per_axis("per_axis"))
    stiff_axis: FloatVectorProperty(
        name="Stiff", description="Spring stiffness along the bone's local X, Y (along the bone), Z",
        size=3, min=0.0, default=(400.0, 400.0, 400.0), subtype="XYZ", override=OVR,
        update=_upd("stiff_axis"))
    damp_axis: FloatVectorProperty(
        name="Damp", description="Damping along the bone's local X, Y (along the bone), Z",
        size=3, min=0.0, default=(1.0, 1.0, 1.0), subtype="XYZ", override=OVR,
        update=_upd("damp_axis"))
    gravity_axis: FloatVectorProperty(
        name="Gravity", description="Gravity multiplier along the bone's local X, Y (along the bone), Z",
        size=3, default=(1.0, 1.0, 1.0), subtype="XYZ", override=OVR,
        update=_upd("gravity_axis"))
    lock: BoolVectorProperty(
        name="Lock",
        description="Freeze motion along the bone's local axis. For the tail only X and Z matter "
                    "(Y is along the bone, use Stretch for that)",
        size=3, default=(False, False, False), subtype="XYZ", override=OVR,
        update=_upd("lock"))

    wind_ob: PointerProperty(name="Wind", description="Wind force field object",
                             type=bpy.types.Object, poll=wind_poll, override=OVR,
                             update=_upd("wind_ob"))
    wind: FloatProperty(name="Wind Multiplier", description="Multiplier for wind forces",
                        default=1.0, override=OVR, update=_upd("wind"))

    collider_type: EnumProperty(
        name="Collider Type",
        items=[("Object", "Object", "Collide with one mesh"),
               ("Collection", "Collection", "Collide with every mesh in a collection")],
        override=OVR, update=_upd("collider_type"))
    collider: PointerProperty(name="Collider Object", description="Mesh to collide with",
                              type=bpy.types.Object, poll=collider_poll, override=OVR,
                              update=_upd("collider"))
    collider_collection: PointerProperty(name="Collider Collection",
                                         description="Collection of meshes to collide with",
                                         type=bpy.types.Collection, override=OVR,
                                         update=_upd("collider_collection"))
    radius: FloatProperty(name="Radius", description="Collision radius",
                          min=0.0, default=0.0, override=OVR, update=_upd("radius"))
    friction: FloatProperty(name="Friction", description="Friction when colliding",
                            min=0.0, soft_max=1.0, default=0.5, override=OVR, update=_upd("friction"))
    bounce: FloatProperty(name="Bounce", description="Bounciness when colliding",
                          min=0.0, soft_max=1.0, default=0.5, override=OVR, update=_upd("bounce"))
    sticky: FloatProperty(name="Sticky", description="Margin beyond the radius that keeps it stuck to the surface",
                          min=0.0, soft_max=1.0, default=0.0, override=OVR, update=_upd("sticky"))

    chain: BoolProperty(name="Chain", description="This bone pulls on its parent, making a physics chain",
                        default=True, override=OVR, update=_upd("chain"))


class EmilsWiggleBoneSettings(bpy.types.PropertyGroup):
    mute: BoolProperty(name="Mute Bone", description="Mute wiggle on this bone",
                       default=False, override=OVR, update=_upd("mute", structure=True))
    use_tail: BoolProperty(name="Tail", description="Wiggle this bone's tail (the bone swings)",
                           default=False, override=OVR, update=_upd("use_tail", structure=True))
    use_head: BoolProperty(name="Head", description="Wiggle this bone's head (the bone moves). "
                                                    "Not available on connected bones",
                           default=False, override=OVR, update=_upd("use_head", structure=True))
    tail: PointerProperty(type=EmilsWiggleSideSettings, override=OVR)
    head: PointerProperty(type=EmilsWiggleSideSettings, override=OVR)


def _upd_object(self, context):
    _settings_changed(context, structure=True)


class EmilsWiggleObjectSettings(bpy.types.PropertyGroup):
    mute: BoolProperty(name="Mute Armature", description="Mute wiggle on this armature",
                       default=False, override=OVR, update=_upd_object)
    freeze: BoolProperty(name="Freeze", description="Wiggle is frozen because this armature was baked",
                         default=False, override=OVR, update=_upd_object)


def _upd_scene_enable(self, context):
    scene = self.id_data
    if self.enabled and not scene.render.use_lock_interface:
        # Renders run on their own thread and we write pose data from it.
        # Lock Interface stops the viewport from evaluating at the same time.
        scene.render.use_lock_interface = True
    runtime.scene_enable_changed(scene)


def _upd_scene_cache(self, context):
    runtime.settings_changed(self.id_data)


class EmilsWiggleSceneSettings(bpy.types.PropertyGroup):
    enabled: BoolProperty(name="Enable Scene", description="Enable Emil's Wiggle on this scene",
                          default=False, override=OVR, update=_upd_scene_enable)
    iterations: IntProperty(name="Quality", description="Constraint solver iterations for chains",
                            min=1, default=2, soft_max=10, max=100, override=OVR,
                            update=_upd_scene_cache)
    substeps: IntProperty(name="Substeps",
                          description="Physics steps per frame. More is more stable with stiff "
                                      "springs and fast motion, but slower",
                          min=1, default=1, soft_max=8, max=32, override=OVR,
                          update=_upd_scene_cache)
    loop: BoolProperty(name="Loop Physics",
                       description="Keep simulating when the timeline loops instead of restarting",
                       default=False, override=OVR, update=_upd_scene_cache)
    preroll: IntProperty(name="Preroll",
                         description="Frames simulated before the start so things are settled",
                         min=0, default=0, override=OVR, update=_upd_scene_cache)
    fast_preview: BoolProperty(
        name="Fast Preview",
        description="While the viewport plays, show each frame's wiggle one frame late so Blender "
                    "only evaluates the scene once. The physics and the cache stay exact, "
                    "renders and paused frames are always exact",
        default=True)
    use_cache: BoolProperty(
        name="Cache",
        description="Remember simulated frames so scrubbing, replays and renders reuse them",
        default=True, update=_upd_scene_cache)
    cache_locked: BoolProperty(
        name="Lock Cache",
        description="Always play the cached frames, even if the scene changed",
        default=False)
    edit_selected: BoolProperty(
        name="Edit All Selected",
        description="Changing a wiggle setting also changes it on every selected bone",
        default=True)

    bake_overwrite: BoolProperty(name="Overwrite Current Action",
                                 description="Bake into the current action instead of a new one",
                                 default=False)
    bake_nla: BoolProperty(name="Current Action to NLA",
                           description="Push the existing animation into an NLA strip first",
                           default=False)


classes = (
    EmilsWiggleSideSettings,
    EmilsWiggleBoneSettings,
    EmilsWiggleObjectSettings,
    EmilsWiggleSceneSettings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.PoseBone.emils_wiggle = PointerProperty(type=EmilsWiggleBoneSettings, override=OVR)
    bpy.types.Object.emils_wiggle = PointerProperty(type=EmilsWiggleObjectSettings, override=OVR)
    bpy.types.Scene.emils_wiggle = PointerProperty(type=EmilsWiggleSceneSettings, override=OVR)


def unregister():
    del bpy.types.Scene.emils_wiggle
    del bpy.types.Object.emils_wiggle
    del bpy.types.PoseBone.emils_wiggle
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
