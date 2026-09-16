"""
Emil's Wiggle - sidebar panels (View3D > Sidebar > Emil).
"""

import bpy

from . import debug, handlers, legacy, runtime


class EmilsWigglePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Emil"


def _bone_ready(context):
    scene = context.scene
    ob = context.object
    pb = context.active_pose_bone
    return (scene.emils_wiggle.enabled and ob is not None and ob.type == "ARMATURE"
            and not ob.emils_wiggle.mute and not ob.emils_wiggle.freeze
            and pb is not None and not pb.emils_wiggle.mute)


def _wiggle2_active(scene):
    return legacy.wiggle2_active() and bool(getattr(scene, "wiggle_enable", False))


def draw_side(layout, context, side, is_tail):
    layout.use_property_split = True
    layout.use_property_decorate = False

    col = layout.column(align=True)
    col.prop(side, "mass")
    col.prop(side, "stiff_axis" if side.per_axis else "stiff")
    col.prop(side, "stretch")
    col.prop(side, "damp_axis" if side.per_axis else "damp")
    col.separator()
    col.prop(side, "gravity_axis" if side.per_axis else "gravity")
    row = col.row(align=True)
    row.prop(side, "wind_ob")
    sub = row.row(align=True)
    sub.ui_units_x = 4
    sub.prop(side, "wind", text="")

    layout.prop(side, "per_axis")
    row = layout.row(align=True, heading="Lock Axis")
    for i, axis in enumerate("XYZ"):
        if is_tail and i == 1:
            continue
        row.prop(side, "lock", index=i, text=axis, toggle=True)

    col = layout.column(align=True)
    col.prop(side, "collider_type", text="Collisions")
    colliding = False
    row = col.row(align=True)
    if side.collider_type == "Object":
        row.prop_search(side, "collider", context.scene, "objects", text=" ")
        if side.collider is not None:
            if context.scene.objects.get(side.collider.name) == side.collider:
                colliding = True
            else:
                row.label(text="", icon="UNLINKED")
    else:
        row.prop_search(side, "collider_collection", bpy.data, "collections", text=" ")
        coll = side.collider_collection
        if coll is not None:
            if coll in context.scene.collection.children_recursive:
                colliding = True
            else:
                row.label(text="", icon="UNLINKED")
    if colliding:
        col = layout.column(align=True)
        for p in ("radius", "friction", "bounce", "sticky"):
            col.prop(side, p)
    layout.prop(side, "chain")


class EMILSWIGGLE_PT_main(EmilsWigglePanel, bpy.types.Panel):
    bl_label = "Emil's Wiggle"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = scene.emils_wiggle
        rt = runtime.peek(scene)

        if rt is not None and rt.legacy_found:
            box = layout.box()
            box.label(text="Wiggle 2 settings found", icon="INFO")
            box.operator("emils_wiggle.import_wiggle2", icon="IMPORT")

        row = layout.row()
        icon = "SCENE_DATA" if s.enabled else "HIDE_ON"
        row.prop(s, "enabled", icon=icon, text="", emboss=False)
        if not s.enabled:
            row.label(text="Scene off")
            return

        if not scene.render.use_lock_interface:
            box = layout.box()
            box.label(text="Lock Interface is off, renders could crash", icon="ERROR")
            box.operator("emils_wiggle.lock_interface", icon="LOCKED")
        if _wiggle2_active(scene):
            box = layout.box()
            box.label(text="Wiggle 2 is also on for this scene", icon="ERROR")
            box.prop(scene, "wiggle_enable", text="Turn Wiggle 2 off", invert_checkbox=True)

        ob = context.object
        if ob is None or ob.type != "ARMATURE":
            row.label(text="Select an armature")
            return
        os_ = ob.emils_wiggle
        if os_.freeze:
            row.prop(os_, "freeze", icon="FREEZE", icon_only=True, emboss=False)
            row.label(text="Frozen after bake")
            return
        icon = "HIDE_ON" if os_.mute else "ARMATURE_DATA"
        row.prop(os_, "mute", icon=icon, icon_only=True, invert_checkbox=True, emboss=False)
        if os_.mute:
            row.label(text="Armature muted")
            return
        pb = context.active_pose_bone
        if pb is None:
            row.label(text="Select a pose bone")
            return
        icon = "HIDE_ON" if pb.emils_wiggle.mute else "BONE_DATA"
        row.prop(pb.emils_wiggle, "mute", icon=icon, icon_only=True, invert_checkbox=True, emboss=False)
        row.label(text="Bone muted" if pb.emils_wiggle.mute else pb.name)


class EMILSWIGGLE_PT_tail(EmilsWigglePanel, bpy.types.Panel):
    bl_label = ""
    bl_parent_id = "EMILSWIGGLE_PT_main"
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    @classmethod
    def poll(cls, context):
        return _bone_ready(context)

    def draw_header(self, context):
        self.layout.prop(context.active_pose_bone.emils_wiggle, "use_tail", text="Tail")

    def draw(self, context):
        s = context.active_pose_bone.emils_wiggle
        if s.use_tail:
            draw_side(self.layout, context, s.tail, True)


class EMILSWIGGLE_PT_head(EmilsWigglePanel, bpy.types.Panel):
    bl_label = ""
    bl_parent_id = "EMILSWIGGLE_PT_main"
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    @classmethod
    def poll(cls, context):
        return _bone_ready(context) and not context.active_pose_bone.bone.use_connect

    def draw_header(self, context):
        self.layout.prop(context.active_pose_bone.emils_wiggle, "use_head", text="Head")

    def draw(self, context):
        s = context.active_pose_bone.emils_wiggle
        if s.use_head:
            draw_side(self.layout, context, s.head, False)


class EMILSWIGGLE_PT_simulation(EmilsWigglePanel, bpy.types.Panel):
    bl_label = "Simulation & Cache"
    bl_parent_id = "EMILSWIGGLE_PT_main"

    @classmethod
    def poll(cls, context):
        return context.scene.emils_wiggle.enabled

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        scene = context.scene
        s = scene.emils_wiggle

        col = layout.column(align=True)
        col.prop(s, "iterations")
        col.prop(s, "substeps")
        col.prop(s, "preroll")
        layout.prop(s, "loop")
        layout.prop(s, "fast_preview")
        layout.prop(s, "use_cache")

        count, first, last = runtime.cache_info(scene)
        box = layout.box()
        col = box.column(align=True)
        if count:
            col.label(text=f"Cached: {count} frames ({first} to {last})", icon="CHECKMARK")
        else:
            col.label(text="Nothing cached yet", icon="INFO")
        here = runtime.frame_is_cached(scene, scene.frame_current)
        col.label(text=f"Frame {scene.frame_current}: " + ("cached" if here else "not cached"),
                  icon="KEYFRAME_HLT" if here else "KEYFRAME")

        row = box.row(align=True)
        row.prop(s, "cache_locked", icon="LOCKED" if s.cache_locked else "UNLOCKED", toggle=True)
        row.operator("emils_wiggle.clear_cache", icon="TRASH", text="")

        col = layout.column(align=True)
        col.operator("emils_wiggle.simulate", icon="PLAY")
        col.operator("emils_wiggle.reset", icon="FILE_REFRESH")


class EMILSWIGGLE_PT_utilities(EmilsWigglePanel, bpy.types.Panel):
    bl_label = "Utilities"
    bl_parent_id = "EMILSWIGGLE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        s = context.scene.emils_wiggle
        layout.prop(s, "edit_selected")
        col = layout.column(align=True)
        if context.mode == "POSE":
            col.operator("emils_wiggle.copy", icon="COPYDOWN")
            col.operator("emils_wiggle.select", icon="RESTRICT_SELECT_OFF")
        col.operator("emils_wiggle.import_wiggle2", icon="IMPORT")


class EMILSWIGGLE_PT_bake(EmilsWigglePanel, bpy.types.Panel):
    bl_label = "Bake to Keyframes"
    bl_parent_id = "EMILSWIGGLE_PT_utilities"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return context.scene.emils_wiggle.enabled and context.mode == "POSE"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        s = context.scene.emils_wiggle
        layout.label(text="Not needed for renders anymore", icon="INFO")
        layout.prop(s, "bake_overwrite")
        row = layout.row()
        row.enabled = not s.bake_overwrite
        row.prop(s, "bake_nla")
        layout.operator("emils_wiggle.bake", icon="KEYTYPE_KEYFRAME_VEC")


class EMILSWIGGLE_PT_debug(EmilsWigglePanel, bpy.types.Panel):
    bl_label = "Debug"
    bl_parent_id = "EMILSWIGGLE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return debug.debug_enabled(context)

    def draw(self, context):
        layout = self.layout
        rt = runtime.peek(context.scene)
        col = layout.column(align=True)
        if rt is None:
            col.label(text="Not running on this scene")
        else:
            col.label(text=f"Last step: {rt.stats_sim_ms:.2f} ms", icon="TIME")
            for key in ("CACHE", "SIM", "RESET", "SAME", "fast", "converged", "errors"):
                if rt.counts.get(key):
                    col.label(text=f"{key}: {rt.counts[key]}")
            for rig in rt.rigs.values():
                fast = "ok" if rig.fast_ok else "not possible"
                bones = f"{len(rig.bones)} bone{'' if len(rig.bones) == 1 else 's'}"
                col.label(text=f"{rig.name}: {bones}, fast preview {fast}",
                          icon="ARMATURE_DATA")
        if handlers.error_count:
            col.label(text=f"{handlers.error_count} errors, see the report", icon="ERROR")
        row = layout.row(align=True)
        row.operator("emils_wiggle.copy_report", icon="COPYDOWN")
        row.operator("emils_wiggle.open_crash_log", icon="FILE_TEXT", text="")
        row.operator("emils_wiggle.reset_counters", icon="LOOP_BACK", text="")


classes = (
    EMILSWIGGLE_PT_main,
    EMILSWIGGLE_PT_tail,
    EMILSWIGGLE_PT_head,
    EMILSWIGGLE_PT_simulation,
    EMILSWIGGLE_PT_utilities,
    EMILSWIGGLE_PT_bake,
    EMILSWIGGLE_PT_debug,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
