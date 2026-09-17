# Changelog

## 1.3.0 (2026-09-16)

Wiggle Groups, and a bunch of cache bugs that made the wiggle twitch or stutter.

- The cache got thrown away every time you pressed play, hit an arrow key or jumped frames. Blender sends one more update after a UI frame change that says everything animated changed, and the add-on believed it. So the cache (Background Cache included) almost never survived real use. Those updates get recognized now.
- Bones could twitch for a frame and then wobble, in the same spot every loop, with no gravity and a parent that doesn't move. Changing a setting (like turning gravity off) clears the cache, but the sim goes on from where the bones are (still bent by the old gravity), and those frames got saved as if they belonged to the run from the first frame. The next loop replayed them in the middle of its own run. They're kept apart now, and the next loop or the Background Cache redoes them properly. That's also why changing a setting back and forth seemed to fix it, it just threw those frames away.
- Fast Preview shows cached frames one frame late too, like the frames it simulates. Before, playback skipped one frame of wiggle when it reached cached frames and repeated one when it left them, a small stutter wherever the cache had a gap.
- Stopping playback made the cache clear itself when an object hangs off a wiggle bone.
- A crash after undo: the empties get made by timers, after Blender's own undo step, and undoing right then could leave a freed empty listed in the helper collection (same for the Background Cache's hidden scene). Blender crashes as soon as something reads a collection like that. Both get cleaned up right after every undo now, and nothing writes to a collection in that state.
- Right after an undo a pose can be missing its bones for a moment, setting the rig up then failed. It waits for Blender now.
- Default Stiff is 200 now (was 400). Wiggle 2 files still import with Wiggle 2's 400 where it never saved a value.

- Wiggle Groups: a list of named bone selections per armature, in pose mode. Click a group and its bones get selected, then the usual Tail/Head settings edit all of them at once (with Edit All Selected on). A bone is in one group at most. Add, remove, assign, remove from group, select and deselect buttons, and the main panel shows which group the active bone is in.
- Selecting, deselecting or hiding bones in pose mode, or clicking a group, cleared the whole cache because Blender reports it like an edit of the armature. The add-on now compares the pose, the rest bones, the object matrix and the constraints, and only a real change clears the cache.
- Slow playback could make the wiggle look like it reset in the middle of the animation, seemingly at random. When frames got skipped, the guessed frames jumped onto any exact frame in the cache, even one from another run (like the one the Background Cache made from the first frame while you were paused somewhere else). They only go back onto the run they were guessed from now, and only right after.
- A single frame taking more than a second during playback restarted the wiggle from rest. Playback keeps simulating through it now.
- The debug report says why a frame started over (a jump, playback skipping ahead...), which frames came late during playback, and counts the frame change updates it ignored.
- Copy Settings to Selected doesn't move bones between groups.
- The debug report lists the groups and which group every wiggle bone is in.

## 1.2.0 (2026-09-16)

Background caching, and slow playback that doesn't fall apart.

- Background Cache: leave Blender alone for 15 seconds and the frames that aren't cached yet get simulated quietly, in a hidden scene that only has copies of the rigs, so your timeline and viewport never move and heavy meshes aren't evaluated. It works in small slices, stops as soon as you do anything, and its frames are exactly what playback gives. It can be turned off in the add-on preferences or the cache box.
- Slow playback that drops frames restarted the wiggle whenever it skipped more than 4 frames (with Loop Physics off it looked like rewinding). It keeps simulating through skipped frames now, and those guessed frames never replace exact ones in the cache.
- Renders with a Frame Step bigger than 4 restarted the wiggle on every frame, they don't anymore.
- When playback loops while dropping frames, the sim goes on from the cached first frame instead of starting from rest wherever it landed.
- The panel says why Fast Preview can't be used on a rig, and the Debug panel shows the real playback fps (the physics time never changed with Fast Preview, that's normal) and why the cache got cleared.
- Changing the frame rate or the gravity clears the cache now.
- Deleting some unrelated object doesn't clear the cache anymore (Blender marks every collection as changed when an object goes away).
- Warnings right in the bone settings when values don't make sense: Bounce or Friction over 1 with a collider, gravity or wind way too strong for the bone, a zero scale, and a note when Stiff or Damp are past the point where they change anything (at the scene's frame rate, substeps and quality). When the sim does blow up and start over, the main panel says so, with the frame and which bones.
- Really extreme settings could blow a wiggle offset up so much that Blender turned it into an infinite scale on the empty. That counts as blown up now (the sim resets), and the empties never get inf or nan values no matter what.
- Saving could crash Blender when the file had library overrides and Wiggle 2 had been turned off during the session (its leftover properties point at freed memory). The stress test found it, and those leftovers are now removed before every save, after loading a file and when Emil's Wiggle starts, on top of the cleanup that already happened.

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
