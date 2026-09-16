"""
Run every Emil's Wiggle test suite on Blender 3.6 and print a summary.

    python tests/run_all.py                 # background suite + GUI suite
    python tests/run_all.py --headless      # just the background suite
    python tests/run_all.py --gui           # just the GUI suite (opens a window)
    python tests/run_all.py --blender PATH  # pick the Blender to use

Plain Python, no bpy needed. Exit code is 0 when everything passed.
Blender is found through --blender, the BLENDER_36 environment variable, the
Microsoft Store launcher, a regular install, or PATH, in that order.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = {
    "headless": ("run_tests.py", ["-b", "--factory-startup"], 600),
    # no --factory-startup: the GUI suite gets its own config folder (see gui_config)
    "gui": ("run_gui_tests.py", ["--no-window-focus", "--window-geometry", "0", "0", "900", "700"], 300),
}

# Makes the throwaway preferences the GUI suite runs with. The tablet API is Windows Ink
# because a stuck tablet driver (Huion's wintab32.dll) crashes Blender as soon as a window opens.
CONFIG_SCRIPT = """
import os, sys, bpy
p = bpy.context.preferences
p.inputs.tablet_api = "WINDOWS_INK"
p.use_preferences_save = False
bpy.ops.wm.save_userpref()
open(sys.argv[sys.argv.index("--") + 1], "w").write("ok")
os._exit(0)
"""


def find_blender(explicit):
    candidates = [explicit, os.environ.get("BLENDER_36")]
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += sorted(glob.glob(os.path.join(
        local, "Microsoft", "WindowsApps", "BlenderFoundation.Blender3.6LTS_*", "blender-launcher.exe")))
    candidates += sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender 3.6*\blender.exe"))
    candidates.append(shutil.which("blender"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def blender_pids():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq blender.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
    except OSError:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == "blender.exe" and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def run_blender(blender, args, result, timeout, env=None):
    """Start Blender and wait until it writes `result`. Returns a status string."""
    if os.path.exists(result):
        os.remove(result)
    # The Store launcher hands off to blender.exe and returns right away,
    # so watch for the result file and for the new blender process dying instead.
    detached = "launcher" in os.path.basename(blender).lower()
    before = blender_pids() if detached else set()
    started = time.time()
    proc = subprocess.Popen([blender, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    seen = set()
    gone_checks = 0
    while time.time() - started < timeout:
        if os.path.exists(result):
            time.sleep(0.5)  # let Blender finish writing
            return "done"
        if detached:
            alive = blender_pids() - before
            if alive:
                seen |= alive
                gone_checks = 0
            elif seen:
                # tasklist sometimes comes back empty for a moment, so be sure before calling it
                gone_checks += 1
                if gone_checks >= 4 and not os.path.exists(result):
                    return "Blender closed without writing results (crash?)"
        elif proc.poll() is not None:
            return f"Blender exited with code {proc.returncode} without writing results"
        time.sleep(0.5)
    return f"timed out after {timeout}s"


def gui_config(blender, out_dir):
    folder = os.path.join(out_dir, "gui_config")
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(os.path.join(folder, "userpref.blend")):
        return folder
    script = os.path.join(out_dir, "make_gui_config.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(CONFIG_SCRIPT)
    env = dict(os.environ, BLENDER_USER_CONFIG=folder)
    marker = os.path.join(out_dir, "gui_config.done")
    for _attempt in range(2):  # a Blender you open or close meanwhile can confuse the process watch
        status = run_blender(blender, ["-b", "--factory-startup", "--python", script, "--", marker], marker, 60, env)
        if status == "done":
            return folder
    raise RuntimeError(f"couldn't make the GUI test config: {status}")


def run_suite(blender, name, out_dir):
    script, args, timeout = SUITES[name]
    result = os.path.join(out_dir, f"{name}.txt")
    log = result + ".log"
    env = None
    if name == "gui":
        env = dict(os.environ, BLENDER_USER_CONFIG=gui_config(blender, out_dir))
    started = time.time()
    status = run_blender(blender, [*args, "--python", os.path.join(HERE, script), "--", result], result, timeout, env)
    elapsed = time.time() - started

    lines = []
    if status == "done":
        with open(result, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    fails = [ln for ln in lines if ln.startswith("FAIL") or ln.startswith("CRASHED") or ln == "TIMEOUT"]
    passes = [ln for ln in lines if ln.startswith("PASS")]
    errors = [ln for ln in lines if ln.startswith("handler errors:") and ln.strip() != "handler errors: 0"]
    ok = status == "done" and not fails and not errors and passes
    return {
        "name": name, "ok": bool(ok), "status": status, "passes": len(passes), "fails": fails + errors,
        "lines": lines, "log": log, "elapsed": elapsed,
    }


def tail(path, count=25):
    if not os.path.exists(path):
        return "(no log)"
    with open(path, encoding="utf-8", errors="replace") as f:
        return "\n".join(f.read().splitlines()[-count:])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blender", help="path to Blender 3.6 (or its Store launcher)")
    parser.add_argument("--headless", action="store_true", help="only the background suite")
    parser.add_argument("--gui", action="store_true", help="only the GUI suite")
    parser.add_argument("--out", help="folder for results and logs (default: a temp folder)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every test line")
    opts = parser.parse_args()

    blender = find_blender(opts.blender)
    if blender is None:
        print("Couldn't find Blender 3.6, pass --blender or set BLENDER_36")
        return 2
    names = [n for n, on in (("headless", opts.headless), ("gui", opts.gui)) if on] or ["headless", "gui"]
    out_dir = opts.out or tempfile.mkdtemp(prefix="emils_wiggle_tests_")
    os.makedirs(out_dir, exist_ok=True)
    print(f"Blender: {blender}\nResults: {out_dir}")

    all_ok = True
    for name in names:
        print(f"\n== {name} ...", flush=True)
        res = run_suite(blender, name, out_dir)
        all_ok &= res["ok"]
        for ln in res["lines"]:
            if opts.verbose or not ln.startswith("PASS"):
                print("  " + ln)
        verdict = "OK" if res["ok"] else "FAILED"
        print(f"== {name}: {verdict}, {res['passes']} passed, {len(res['fails'])} failed"
              f" ({res['elapsed']:.0f}s){'' if res['status'] == 'done' else ', ' + res['status']}")
        if not res["ok"]:
            print(f"-- last lines of {res['log']}:\n{tail(res['log'])}")

    print("\nALL GOOD" if all_ok else "\nSOMETHING FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
