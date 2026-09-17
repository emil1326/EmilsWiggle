# Emil's Wiggle

Bone spring physics add-on, a rewrite of Wiggle 2. **Blender 3.6 LTS only**, don't add 4.x/5.x code. README.md explains the features and the architecture.

## Dev loop

Emil tests in his real Blender 3.6, reports problems (often by pasting a Debug Report), I fix them.

- The add-on is live-linked: `%LOCALAPPDATA%\Packages\BlenderFoundation.Blender3.6LTS_ppwjx1n5r4v9t\LocalCache\Roaming\Blender Foundation\Blender\3.6\scripts\addons\EmilsWiggle` is a junction to this folder. After a change Emil only needs F3 > Reload Scripts. Don't delete that junction with anything recursive.
- Every bug that gets fixed also gets a regression test: `tests/run_tests.py` for anything background mode can reach, `tests/run_gui_tests.py` for real playback or threaded F12/Ctrl+F12 renders. Repro `.blend` files go in `tests/scenes/`.
- `python tests/run_all.py --stress` (random edits/renders/undo/reloads, background + GUI) goes before releases or after touching runtime.py; a crash leaves the last action in `<result>.log`.
- Run `python tests/run_all.py` before every commit. I run it, not Emil. It drives the Store install of Blender 3.6 by itself (background suite ~1 min, GUI suite opens its own small window ~30 s). `--headless` / `--gui` run one suite, `-v` prints every line.
- Releases: bump `bl_info["version"]`, add a CHANGELOG entry, `python tools/build_zip.py` (writes `../dist/EmilsWiggle-<version>.zip`, leaves tests/tools/dev files out).
- The Debug panel only shows with Preferences > Add-ons > Emil's Wiggle > Developer Tools. Its Copy Debug Report text comes from `debug.build_report`, keep it useful when adding features.

## Rules that bit me

- Never write bone loc/rot/scale from a frame handler. During renders that makes Blender re-copy the armature and lose animated values and the object matrix (3.6 has the animation backup switched off). The wiggle goes through the "Emil's Wiggle" Copy Transforms constraint (owner LOCAL, target WORLD, mix AFTER_FULL) and a helper empty; an empty's target space must be WORLD, LOCAL gives wrong rotations.
- Handlers also run on the render thread: only use the `scene`/`depsgraph` arguments, never `bpy.context`, don't create or delete data while `rt.rendering`, and move helpers through `HelperWriter` (foreach_set + update_tag) because normal RNA writes push UI notifiers from that thread.
- Matrices read from RNA (`pb.matrix_basis`, `ob.matrix_world`...) are live views. `.copy()` them before keeping them around.
- A GUI Blender that dies ~1 s after starting (0xC0000005, no crash log) is the Huion tablet's wintab32.dll, not this add-on. `run_all.py` gives the GUI suite its own config with Tablet API = Windows Ink for that reason, keep it that way.
- Never create/delete objects or constraints in `frame_change_pre`: a window scene switch builds the depsgraph, runs pre, builds again without evaluating, and 3.6 crashes in `update_invalid_cow_pointers` (unexpanded CoW copy). Pre does `rebuild(edit=False)`, the edits happen in `frame_change_post` or the `request_edits()` timer.
- Only the main thread may create/delete data (`runtime._can_edit_data`): renders, background Alembic/USD exports and compositor scenes run the frame handlers on other threads.
- `background.py`: never use `ViewLayer.update()` or `context.evaluated_depsgraph_get()` on the hidden scene (they make its depsgraph active and results get copied onto the real objects), only `Scene.frame_set` once and `Depsgraph.update()`. Any depsgraph update pokes every 3D view's draw engines, so no background work while a viewport is in Rendered shading. Don't keep bpy references across timer ticks, re-resolve by pointer.
- `depsgraph.id_type_updated()` is shared by all depsgraphs, and deleting any object tags every collection. Don't treat that as "collections changed", compare what actually matters (`runtime._collider_state`).
- Selecting bones tags the armature (and the object) like a real edit, and any add-on property edited from the UI (or from Python when it has an update callback) tags its owner ID with TRANSFORM|GEOMETRY. `runtime._only_clicked` compares pose/rest/matrix/constraints in pose mode so those don't clear the cache; our own group props call `runtime.note_click`. Writing the same value again still counts as an edit, and `bpy.ops` runs `view_layer.update()` before every operator, so tests must only write selection that actually changes.
- `frame_change_pre` gets no depsgraph in 3.6. Render animation calls the frame handlers once per view layer, and `scene.frame_set` calls them once per view layer too.
- Writing text (README, CHANGELOG, UI strings) follows Emil's voice from the global CLAUDE.md.
