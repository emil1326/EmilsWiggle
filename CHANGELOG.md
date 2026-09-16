# Changelog

## 1.1.0 (2026-09-16)

Stability pass. Blender was crashing on me and I wanted this thing rock solid, so it got a stress test that throws hundreds of random edits, renders, undos and file reloads at it, plus fixes for everything a code review turned up.

- Loop Physics is off by default now.
- Turning Wiggle 2 off while Blender runs left broken properties behind that could crash Blender later, those get removed automatically.
- A Crash Log (on by default) writes what Python was doing when Blender dies.
- Renders right after opening a file simulate again, animated settings render right, and motion blur works.
- Duplicated bones, copied rigs and scene copies get their own empties, and nothing deletes an empty another rig still uses.
- The sim resets itself instead of blowing up with extreme settings or a zero scale.
- Renamed, deleted or reparented bones, collider changes and our own empty moves are all handled without clearing the cache for nothing.
- One broken rig or armature can't stop the others anymore.
- New Scene > Full Copy (or switching to a scene you never looked at) crashed Blender when that scene had wiggle rigs. Empties and constraints aren't made in frame_change_pre anymore, which is what 3.6 choked on.
- Alembic/USD exports running in the background and scenes rendered through the compositor call the frame handlers from their own thread, those don't create or delete anything now either.
- A copied rig never moves the original rig's empties, even for the one frame before it gets its own.
- Turning Wiggle 2 off cleans up its leftovers right away instead of within a second.
- Preroll stops once the bones are settled, and the cache has a memory limit for very long timelines.

## 1.0.0 (2026-09-16)

First version, forked from Wiggle 2 2.2.4 by Steve Miller and pretty much rebuilt around the same physics.

Wiggle works in F12, Ctrl+F12 and command line renders now, no bake needed, and renders match the viewport exactly (the wiggle goes through a constraint and a hidden empty instead of the bone channels). Simulated frames get cached, so scrubbing, replays and renders reuse them, and you can lock the cache or fill it with Simulate Range.

New stuff: per axis stiffness, damping and gravity, axis locks for tail and head, Fast Preview for viewport playback, substeps, loops that replay once they've settled, and an importer for Wiggle 2 settings.

Fixed: the timestep ignoring `fps_base`, muted armatures still costing a bunch of fps, the sim state living in bone properties, scaled armatures wiggling wrong, swings flipping, wiggle bones not being poseable, pops on frame jumps, preview range loops, faceless colliders killing the sim, and handlers reading the wrong scene. The README has the long version.
