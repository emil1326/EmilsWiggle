# Changelog

## 1.0.0 (2026-09-16)

First version, forked from Wiggle 2 2.2.4 by Steve Miller and pretty much rebuilt around the same physics.

Wiggle works in F12, Ctrl+F12 and command line renders now, no bake needed, and renders match the viewport exactly (the wiggle goes through a constraint and a hidden empty instead of the bone channels). Simulated frames get cached, so scrubbing, replays and renders reuse them, and you can lock the cache or fill it with Simulate Range.

New stuff: per axis stiffness, damping and gravity, axis locks for tail and head, Fast Preview for viewport playback, substeps, loops that replay once they've settled, and an importer for Wiggle 2 settings.

Fixed: the timestep ignoring `fps_base`, muted armatures still costing a bunch of fps, the sim state living in bone properties, scaled armatures wiggling wrong, swings flipping, wiggle bones not being poseable, pops on frame jumps, preview range loops, faceless colliders killing the sim, and handlers reading the wrong scene. The README has the long version.
