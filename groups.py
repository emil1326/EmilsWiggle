"""
Emil's Wiggle - Wiggle Groups.

A Wiggle Group is a named set of bones on an armature, only there to select them
together in pose mode. The settings stay on the bones: pick a group, edit the active
bone and Edit All Selected carries the change to the rest of the group.

Each bone stores the number (uid) of its group, 0 for none, so a bone is in one group
at most and renaming a group or a bone doesn't lose anything. Nothing here touches
the simulation.
"""

from . import runtime

_quiet = False


class quiet:
    """Change the active group without selecting its bones."""

    def __enter__(self):
        global _quiet
        self._prev = _quiet
        _quiet = True

    def __exit__(self, *exc):
        global _quiet
        _quiet = self._prev
        return False


def active(ob):
    s = ob.emils_wiggle
    if 0 <= s.active_group < len(s.groups):
        return s.groups[s.active_group]
    return None


def find(ob, uid):
    if uid <= 0:
        return None
    for g in ob.emils_wiggle.groups:
        if g.uid == uid:
            return g
    return None


def members(ob, uid):
    if uid <= 0:
        return []
    return [pb for pb in ob.pose.bones if pb.emils_wiggle.group == uid]


def counts(ob):
    out = {}
    for pb in ob.pose.bones:
        uid = pb.emils_wiggle.group
        if uid:
            out[uid] = out.get(uid, 0) + 1
    return out


def new_group(ob, name="Group"):
    s = ob.emils_wiggle
    # above every number in use, bones pointing at a deleted group included
    uid = max([0] + [g.uid for g in s.groups] + [pb.emils_wiggle.group for pb in ob.pose.bones]) + 1
    names = {g.name for g in s.groups}
    final, i = name, 1
    while final in names:
        final = f"{name}.{i:03d}"
        i += 1
    g = s.groups.add()
    g.name = final
    g.uid = uid
    with quiet():
        s.active_group = len(s.groups) - 1
    return g


def remove_group(ob, index):
    """Delete a group, its bones just stop being in one. False when Blender won't
    (a group that comes from the linked file of an overridden rig)."""
    s = ob.emils_wiggle
    uid = s.groups[index].uid
    try:
        s.groups.remove(index)
    except Exception:
        return False
    if find(ob, uid) is None:
        for pb in members(ob, uid):
            pb.emils_wiggle.group = 0
    with quiet():
        s.active_group = min(s.active_group, max(0, len(s.groups) - 1))
    return True


def _depth(pb):
    d = 0
    while pb.parent is not None:
        pb = pb.parent
        d += 1
    return d


def visible(pb):
    bone = pb.bone
    return not bone.hide and any(a and b for a, b in zip(bone.layers, pb.id_data.data.layers))


def _set_select(bone, value):
    if bone.select != value:
        bone.select = value
    if not value:
        if bone.select_head:
            bone.select_head = False
        if bone.select_tail:
            bone.select_tail = False


def _pose_objects(context, ob):
    objs = [o for o in (getattr(context, "objects_in_mode", None) or ())
            if o.type == "ARMATURE" and o.mode == "POSE"]
    if ob not in objs:
        objs.append(ob)
    return objs


def select(context, ob, uid):
    """Select the group's visible bones and nothing else. The active bone stays if it's in
    the group, otherwise the group's top bone becomes active. Returns the selected bones."""
    for other in _pose_objects(context, ob):
        for bone in other.data.bones:
            _set_select(bone, False)
    picked = [pb for pb in members(ob, uid) if visible(pb)]
    for pb in picked:
        _set_select(pb.bone, True)
    arm = ob.data
    act = arm.bones.active
    if picked and (act is None or act.name not in {pb.name for pb in picked}):
        arm.bones.active = min(picked, key=_depth).bone
    return picked


def deselect(ob, uid):
    for pb in members(ob, uid):
        _set_select(pb.bone, False)


def selected_here(context, ob):
    """Selected pose bones that belong to `ob` (other armatures can be in pose mode too)."""
    return [pb for pb in (getattr(context, "selected_pose_bones", None) or ()) if pb.id_data == ob]


def assign(context, ob, uid):
    """Move the selected bones into the group, out of whatever group they were in."""
    bones = selected_here(context, ob)
    for pb in bones:
        if pb.emils_wiggle.group != uid:
            pb.emils_wiggle.group = uid
    return len(bones)


def unassign(context, ob, uid):
    count = 0
    for pb in selected_here(context, ob):
        if pb.emils_wiggle.group == uid:
            pb.emils_wiggle.group = 0
            count += 1
    return count


def on_renamed(group, context):
    runtime.note_click(group.id_data)


def on_active_changed(group_settings, context):
    """Clicking a group in the list selects its bones."""
    ob = group_settings.id_data
    runtime.note_click(ob)
    if _quiet or context is None:
        return
    if getattr(ob, "type", None) != "ARMATURE" or ob.mode != "POSE" or ob.pose is None:
        return
    g = active(ob)
    if g is not None:
        select(context, ob, g.uid)
