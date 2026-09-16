"""
Emil's Wiggle - the physics.

Same spring / stretch / chain / collision model as Wiggle 2 by Steve Miller,
but running on plain Python objects. Nothing in here touches bpy data except
closest_point_on_mesh on colliders, so it's safe to run from the render thread.

Every matrix here is in world space. `b.v_*` values are the evaluated
(un-wiggled) pose for the current substep, prepared by prepare_view().
"""

from mathutils import Matrix, Vector

Y_AXIS = Vector((0.0, 1.0, 0.0))
EPS = 1e-12


class Side:
    """Settings snapshot for one end (tail or head) of a bone."""
    __slots__ = (
        "mass", "stiff", "stretch", "damp", "gravity",
        "per_axis", "stiff_axis", "damp_axis", "gravity_axis", "lock",
        "wind_ob", "wind", "colliders", "collection", "radius", "friction", "bounce", "sticky", "chain",
    )


_MISS = (False, None, None, -1)


class ColliderInfo:
    __slots__ = ("ob", "mw", "mw_inv", "rot", "broken")

    def __init__(self, ob, mw):
        self.ob = ob
        self.broken = False
        self.mw = mw
        self.mw_inv = mw.inverted_safe()
        self.rot = mw.to_quaternion().to_matrix()


class WindInfo:
    __slots__ = ("direction", "strength", "factor")

    def __init__(self, ob_eval):
        self.direction = ob_eval.matrix_world.to_quaternion() @ Vector((0.0, 0.0, 1.0))
        self.strength = ob_eval.field.strength
        self.factor = ob_eval.field.wind_factor


class World:
    """Per-frame global inputs."""
    __slots__ = ("dt", "dt2", "iterations", "gravity", "colliders", "winds", "depsgraph")

    def __init__(self, dt, iterations, gravity, depsgraph):
        self.dt = dt
        self.dt2 = dt * dt
        self.iterations = iterations
        self.gravity = gravity
        self.colliders = {}
        self.winds = {}
        self.depsgraph = depsgraph

    def scaled(self, factor):
        """Same world with longer steps, for frames dropped during slow playback."""
        w = World(self.dt * factor, self.iterations, self.gravity, self.depsgraph)
        w.colliders = self.colliders
        w.winds = self.winds
        return w

    def closest(self, c, pos):
        if c.broken:
            return _MISS
        try:
            return c.ob.closest_point_on_mesh(c.mw_inv @ pos, depsgraph=self.depsgraph)
        except RuntimeError:
            c.broken = True  # e.g. a mesh with no faces, skip it for this frame
            return _MISS


class BoneState:
    __slots__ = (
        # structure
        "name", "parent", "has_tail", "has_head", "connected", "full_scale", "length",
        "q_name", "q_length", "parent_is_q",
        "tail", "head", "has_pin_constraint",
        # per-frame inputs
        "pw", "pw_prev", "qw", "qw_prev", "pin",
        # per-substep view
        "v_pw", "v_pw_inv", "v_len_world", "v_scale", "v_rel", "v_qtail", "v_head_gap", "v_rel_q",
        # simulation state
        "mat", "pos", "pos_last", "vel", "hpos", "hpos_last", "hvel",
        "cp", "co", "cn", "hcp", "hco", "hcn",
        # output: the wiggle offset and the empty that shows it
        "delta", "ready", "helper", "shown", "pre_delta_inv",
    )

    def __init__(self, name):
        self.name = name
        self.parent = None
        self.q_name = None
        self.q_length = 0.0
        self.parent_is_q = False
        self.has_pin_constraint = False
        self.pw = self.pw_prev = self.qw = self.qw_prev = None
        self.pin = None
        self.delta = Matrix.Identity(4)
        self.ready = False
        self.helper = None
        self.shown = None
        self.pre_delta_inv = None


# ---------------------------------------------------------------- helpers

def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _get_fac(m1, m2):
    return 0.5 if m1 == m2 else m1 / (m1 + m2)


def _rot_to(vec):
    if vec.length_squared < EPS:
        return Matrix.Identity(4)
    return Y_AXIS.rotation_difference(vec).to_matrix().to_4x4()


def carried(b):
    """Where the bone would be if it just followed its (wiggled) wiggle parent."""
    if b.parent is None:
        return b.v_pw
    return b.parent.mat @ b.v_rel


def _spring(target, position, side, rot, world):
    s = target - position
    k = world.dt2 / world.iterations
    if not side.per_axis:
        f = side.stiff * k
        return s if f > 1.0 else s * f
    rinv = rot.transposed()
    sl = rinv @ s
    for i in range(3):
        f = side.stiff_axis[i] * k
        if f <= 1.0:
            sl[i] *= f
    return rot @ sl


def _stretch(target, position, fac):
    return (target - position) * (1.0 - fac)


def _damp(vel, side, dt, rot):
    if not side.per_axis:
        return vel * _clamp01(1.0 - side.damp * dt)
    vl = rot.transposed() @ vel
    for i in range(3):
        vl[i] *= _clamp01(1.0 - side.damp_axis[i] * dt)
    return rot @ vl


def _gravity(g, side, rot):
    if not side.per_axis:
        return g * side.gravity
    gl = rot.transposed() @ g
    for i in range(3):
        gl[i] *= side.gravity_axis[i]
    return rot @ gl


def _lock_head(b, frame):
    lk = b.head.lock
    if not (lk[0] or lk[1] or lk[2]):
        return
    rot = frame.to_quaternion().to_matrix()
    origin = frame.translation
    o = rot.transposed() @ (b.hpos - origin)
    for i in range(3):
        if lk[i]:
            o[i] = 0.0
    b.hpos = origin + rot @ o


def _lock_tail(b, frame):
    lk = b.tail.lock
    if not (lk[0] or lk[2]):
        return
    rot = frame.to_quaternion().to_matrix()
    head = b.hpos if b.has_head else frame.translation
    v = rot.transposed() @ (b.pos - head)
    length = v.length
    if lk[0] and lk[2]:
        v = Vector((0.0, length, 0.0))
    else:
        if lk[0]:
            v.x = 0.0
        if lk[2]:
            v.z = 0.0
        vl = v.length
        v = Vector((0.0, length, 0.0)) if vl < 1e-9 else v * (length / vl)
    b.pos = head + rot @ v


def _pin(b):
    if b.pin is not None:
        goal, influence = b.pin
        b.pos = b.pos * (1.0 - influence) + goal * influence


def collide(b, world, head=False):
    side = b.head if head else b.tail
    if head:
        pos, co, cp, cn = b.hpos, b.hco, b.hcp, b.hcn
    else:
        pos, co, cp, cn = b.pos, b.co, b.cp, b.cn
    col = False
    for name in side.colliders:
        c = world.colliders.get(name)
        if c is None:
            continue
        ok, loc, nor, _index = world.closest(c, pos)
        if not ok:
            continue
        n = (c.rot @ nor).normalized()
        hit = c.mw @ loc
        v = hit - pos
        vn = v.normalized()
        d = n.dot(vn)
        vl = v.length
        if d > 0.01 or vl < side.radius or (co is not None and vl < side.radius + side.sticky):
            nv = vn if d > 0.0 else -vn
            pos = hit + nv * side.radius
            if co is not None:
                prev = world.colliders.get(co)
                if prev is not None:
                    pos = pos.lerp(prev.mw @ cp, side.friction)
            col = True
            co = name
            cp = c.mw_inv @ pos
            cn = nv
    if not col:
        co = None
    if head:
        b.hpos, b.hco, b.hcp, b.hcn = pos, co, cp, cn
    else:
        b.pos, b.co, b.cp, b.cn = pos, co, cp, cn


def update_matrix(b):
    p = b.parent
    if p is not None:
        mat = p.mat @ b.v_rel
        if b.full_scale:
            m2 = mat
        else:
            ro = (p.mat.to_quaternion() @ b.v_rel.to_quaternion()).to_matrix().to_4x4()
            sc = Matrix.Diagonal(b.v_scale).to_4x4()
            m2 = Matrix.Translation(mat.translation) @ ro @ sc
    else:
        mat = b.v_pw
        m2 = mat
    head_world = mat.translation

    loc = None
    if b.has_head:
        m2 = Matrix.Translation(b.hpos - m2.translation) @ m2
        loc = mat.inverted_safe() @ b.hpos
        mat = m2

    rot = _rot_to(m2.inverted_safe() @ b.pos)

    if b.has_head:
        sy = (b.hpos - b.pos).length / b.v_len_world if b.v_len_world > EPS else 1.0
    elif b.full_scale:
        sy = (mat.inverted_safe() @ b.pos).length / b.length if b.length > EPS else 1.0
    else:
        sy = (head_world - b.pos).length / b.v_len_world if b.v_len_world > EPS else 1.0

    scale = Matrix.Scale(sy, 4, Y_AXIS)
    delta = rot @ scale
    if loc is not None:
        delta = Matrix.Translation(loc) @ delta
    b.delta = delta
    b.mat = m2 @ rot @ scale


def move(b, world):
    dt = world.dt
    frame = carried(b)
    rot = frame.to_quaternion().to_matrix()
    if b.has_tail:
        t = b.tail
        b.vel = _damp(b.vel, t, dt, rot)
        force = _gravity(world.gravity, t, rot)
        wind = world.winds.get(t.wind_ob) if t.wind_ob else None
        if wind is not None:
            along = (b.pos - b.mat.translation).normalized()
            fac = 1.0 - wind.factor * abs(wind.direction.dot(along))
            force = force + wind.direction * (fac * wind.strength * t.wind / t.mass)
        b.pos = b.pos + b.vel + force * world.dt2
        _pin(b)
    if b.has_head:
        h = b.head
        b.hvel = _damp(b.hvel, h, dt, rot)
        force = _gravity(world.gravity, h, rot)
        wind = world.winds.get(h.wind_ob) if h.wind_ob else None
        if wind is not None:
            force = force + wind.direction * (wind.strength * h.wind / h.mass)
        b.hpos = b.hpos + b.hvel + force * world.dt2
        _lock_head(b, frame)
    if b.has_tail:
        _lock_tail(b, frame)
        collide(b, world)
    if b.has_head:
        collide(b, world, True)
    update_matrix(b)


def constrain(b, i, world):
    p = b.parent
    mat = carried(b)
    frame = mat
    rot = mat.to_quaternion().to_matrix()
    update_p = False
    t, h = b.tail, b.head
    L = b.length

    # spring
    if b.has_head:
        s = _spring(mat.translation, b.hpos, h, rot, world)
        if p is not None and h.chain:
            if p.has_tail:
                fac = _get_fac(h.mass, p.tail.mass) if i else p.tail.stretch
                p.pos = p.pos - s * fac
            else:
                fac = _get_fac(h.mass, p.head.mass)
                p.hpos = p.hpos - s * fac
            b.hpos = b.hpos + s * (1.0 - fac)
        else:
            b.hpos = b.hpos + s

        mat = Matrix.LocRotScale(b.hpos, mat.to_quaternion(), b.v_scale)
        target = mat @ Vector((0.0, L, 0.0))
        if b.has_tail:
            s = _spring(target, b.pos, t, rot, world)
            if t.chain:
                fac = _get_fac(t.mass, h.mass)
                b.hpos = b.hpos - s * fac
                b.pos = b.pos + s * (1.0 - fac)
            else:
                b.pos = b.pos + s
        else:
            b.pos = target
    else:
        mat = Matrix.LocRotScale(mat.translation, mat.to_quaternion(), b.v_scale)
        target = mat @ Vector((0.0, L, 0.0))
        s = _spring(target, b.pos, t, rot, world)
        if p is not None and t.chain and p.has_tail:
            fac = _get_fac(t.mass, p.tail.mass)
            if b.pin is not None:
                fac = 1.0 - t.stretch
            if i == 0:
                fac = p.tail.stretch
            if b.parent_is_q and b.connected:
                p.pos = p.pos - s * fac
            else:
                pt = p.mat.translation
                tailpos = b.mat @ Vector((0.0, L, 0.0))
                v1 = (b.mat.translation + tailpos) * 0.5 - pt
                tailpos = tailpos - s * fac
                v2 = (b.mat.translation + tailpos) * 0.5 - pt
                if v1.length > EPS:
                    q = v1.rotation_difference(v2)
                    p.pos = pt + (q @ (p.pos - pt)) * (v2.length / v1.length)
            b.pos = b.pos + s * (1.0 - fac)
            update_p = True
        else:
            b.pos = b.pos + s

    # stretch
    if b.has_head:
        if p is not None:
            if b.parent_is_q and p.has_tail:
                target = p.pos + (b.hpos - p.pos).normalized() * b.v_head_gap
            elif b.v_rel_q is not None:
                tp = p.mat @ b.v_rel_q @ Vector((0.0, b.q_length, 0.0))
                target = tp + (b.hpos - tp).normalized() * b.v_head_gap
            else:
                target = mat.translation
        elif b.v_qtail is not None:
            qt = b.v_qtail
            target = qt + (b.hpos - qt).normalized() * b.v_head_gap
        else:
            target = mat.translation
        s = _stretch(target, b.hpos, h.stretch)
        if p is not None and h.chain:
            if p.has_tail:
                fac = _get_fac(h.mass, p.tail.mass) if i else p.tail.stretch
                if b.v_rel_q is not None:
                    tailpos = p.mat @ b.v_rel_q @ Vector((0.0, b.q_length, 0.0))
                    denom = (p.mat.translation - tailpos).length
                    ratio = (p.mat.translation - p.pos).length / denom if denom > EPS else 1.0
                else:
                    ratio = 1.0
                p.pos = p.pos - s * (ratio * fac)
            else:
                fac = _get_fac(h.mass, p.head.mass) if i else p.head.stretch
                p.hpos = p.hpos - s * fac
            b.hpos = b.hpos + s * (1.0 - fac)
        else:
            b.hpos = b.hpos + s

        target = b.hpos + (b.pos - b.hpos).normalized() * b.v_len_world
        if b.has_tail:
            s = _stretch(target, b.pos, t.stretch)
            if t.chain:
                fac = _get_fac(t.mass, h.mass) if i else h.stretch
                b.hpos = b.hpos - s * fac
                b.pos = b.pos + s * (1.0 - fac)
            else:
                b.pos = b.pos + s
        else:
            b.pos = target
    else:
        origin = mat.translation
        target = origin + (b.pos - origin).normalized() * b.v_len_world
        s = _stretch(target, b.pos, t.stretch)
        if p is not None and t.chain and p.has_tail:
            fac = _get_fac(t.mass, p.tail.mass)
            if b.pin is not None:
                fac = 1.0 - t.stretch
            if i == 0:
                fac = p.tail.stretch
            if b.parent_is_q and b.connected:
                p.pos = p.pos - s * fac
            else:
                pt = p.mat.translation
                v1 = b.mat.translation - pt
                v2 = (b.mat.translation - s * fac) - pt
                if v1.length > EPS:
                    q = v1.rotation_difference(v2)
                    p.pos = pt + (q @ (p.pos - pt)) * (v2.length / v1.length)
            b.pos = b.pos + s * (1.0 - fac)
            update_p = True
        else:
            b.pos = b.pos + s

    if update_p:
        if p.tail.lock[0] or p.tail.lock[2]:
            _lock_tail(p, carried(p))
        collide(p, world)
        update_matrix(p)
    if b.has_head:
        _lock_head(b, frame)
    if b.has_tail:
        _pin(b)
        _lock_tail(b, frame)
        collide(b, world)
    if b.has_head:
        collide(b, world, True)
    update_matrix(b)


# ---------------------------------------------------------------- frame level

def prepare_view(bones, alpha):
    """Build the evaluated (un-wiggled) pose for this substep. Parents come first."""
    y = Vector((0.0, 1.0, 0.0))
    for b in bones:
        pw = b.pw if (alpha >= 1.0 or b.pw_prev is None) else b.pw_prev.lerp(b.pw, alpha)
        b.v_pw = pw
        b.v_pw_inv = pw.inverted_safe()
        y.y = b.length
        b.v_len_world = (pw @ y - pw.translation).length
        b.v_scale = pw.to_scale()
        b.v_rel = (b.parent.v_pw_inv @ pw) if b.parent is not None else None
        b.v_qtail = b.v_head_gap = b.v_rel_q = None
        if b.has_head and b.qw is not None:
            qw = b.qw if (alpha >= 1.0 or b.qw_prev is None) else b.qw_prev.lerp(b.qw, alpha)
            y.y = b.q_length
            qtail = qw @ y
            b.v_qtail = qtail
            b.v_head_gap = (pw.translation - qtail).length
            if b.parent is not None:
                b.v_rel_q = b.parent.v_pw_inv @ qw


def reset(bones):
    zero = Vector()
    for b in bones:
        pw = b.v_pw
        b.mat = pw.copy()
        b.pos = pw @ Vector((0.0, b.length, 0.0))
        b.pos_last = b.pos.copy()
        b.hpos = pw.translation.copy()
        b.hpos_last = b.hpos.copy()
        b.vel = zero.copy()
        b.hvel = zero.copy()
        b.cp, b.cn, b.co = zero.copy(), zero.copy(), None
        b.hcp, b.hcn, b.hco = zero.copy(), zero.copy(), None
        b.delta = Matrix.Identity(4)
        b.ready = True
    for b in bones:
        update_matrix(b)


def _run(gen):
    """Run a generator from this module to the end, returns its return value."""
    try:
        while True:
            next(gen)
    except StopIteration as done:
        return done.value


def step_iter(bones, world, chunk=0):
    """One physics step of world.dt seconds.

    With chunk > 0 it pauses (yields) every `chunk` bone updates, so the background
    cache can spread one step over several short slices. Same math either way.
    """
    zero = Vector()
    left = chunk
    for b in bones:
        b.cn = zero.copy()
        b.hcn = zero.copy()
        move(b, world)
        if chunk:
            left -= 1
            if left <= 0:
                left = chunk
                yield
    n = world.iterations
    for it in range(n):
        idx = n - 1 - it
        for b in bones:
            constrain(b, idx, world)
            if chunk:
                left -= 1
                if left <= 0:
                    left = chunk
                    yield
    for b in bones:
        update_matrix(b)
    for b in bones:
        vb = zero
        if b.has_tail and b.cn.length_squared > 0.0:
            vb = b.vel.reflect(b.cn).project(b.cn) * b.tail.bounce
        b.vel = (b.pos - b.pos_last) + vb
        vb = zero
        if b.has_head and b.hcn.length_squared > 0.0:
            vb = b.hvel.reflect(b.hcn).project(b.hcn) * b.head.bounce
        b.hvel = (b.hpos - b.hpos_last) + vb
        b.pos_last = b.pos.copy()
        b.hpos_last = b.hpos.copy()


def step(bones, world):
    _run(step_iter(bones, world))


def simulate_iter(bones, world, steps, chunk=0, scale=1.0):
    """Run `steps` steps, sliding the evaluated pose from pw_prev to pw.

    scale > 1 makes every step last longer (dt * scale). Velocities are a distance per
    step here, so they get stretched to the longer step first and back at the end.
    """
    if scale != 1.0:
        world = world.scaled(scale)
        for b in bones:
            b.vel = b.vel * scale
            b.hvel = b.hvel * scale
    for k in range(1, steps + 1):
        prepare_view(bones, k / steps)
        yield from step_iter(bones, world, chunk)
    if scale != 1.0:
        for b in bones:
            b.vel = b.vel / scale
            b.hvel = b.hvel / scale


def simulate(bones, world, steps, scale=1.0):
    _run(simulate_iter(bones, world, steps, 0, scale))


SETTLE_WINDOW = 10
SETTLE_TOL = 1e-5  # of the bone's world length


def _still(bones, ref):
    for b, (pos, hpos) in zip(bones, ref):
        tol = SETTLE_TOL * b.v_len_world
        if ((b.pos - pos).length > tol or b.vel.length > tol
                or (b.hpos - hpos).length > tol or b.hvel.length > tol):
            return False
    return True


def settle_iter(bones, world, steps, chunk=0):
    """Preroll: simulate with the pose held still. Returns how many steps it took.

    Nothing changes between steps but the bones themselves, so once they stop moving
    for a whole window the rest of the preroll would do nothing and gets skipped.
    """
    prepare_view(bones, 1.0)
    ref = None
    for i in range(steps):
        yield from step_iter(bones, world, chunk)
        if (i + 1) % SETTLE_WINDOW == 0:
            if ref is not None and _still(bones, ref):
                return i + 1
            ref = [(b.pos.copy(), b.hpos.copy()) for b in bones]
    return steps


def settle(bones, world, steps):
    return _run(settle_iter(bones, world, steps))


# ---------------------------------------------------------------- snapshots

def snapshot(b):
    return (
        b.mat.copy(), b.pos.copy(), b.pos_last.copy(), b.vel.copy(),
        b.hpos.copy(), b.hpos_last.copy(), b.hvel.copy(),
        b.cp.copy(), b.co, b.cn.copy(), b.hcp.copy(), b.hco, b.hcn.copy(),
        b.delta.copy(), b.pw.copy(), None if b.qw is None else b.qw.copy(),
    )


def restore(b, snap):
    (mat, pos, pos_last, vel, hpos, hpos_last, hvel,
     cp, co, cn, hcp, hco, hcn, delta, pw, qw) = snap
    b.mat, b.pos, b.pos_last, b.vel = mat.copy(), pos.copy(), pos_last.copy(), vel.copy()
    b.hpos, b.hpos_last, b.hvel = hpos.copy(), hpos_last.copy(), hvel.copy()
    b.cp, b.co, b.cn = cp.copy(), co, cn.copy()
    b.hcp, b.hco, b.hcn = hcp.copy(), hco, hcn.copy()
    b.delta = delta.copy()
    b.pw = pw.copy()
    b.qw = None if qw is None else qw.copy()
    b.ready = True
