"""
Build the installable zip for Emil's Wiggle.

    python tools/build_zip.py            # -> ../dist/EmilsWiggle-<version>.zip
    python tools/build_zip.py --out DIR

Leaves out tests, tools, git files and dev notes. The version comes from bl_info.
"""

import argparse
import ast
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "EmilsWiggle"
SKIP_DIRS = {"tests", "tools", "BlenderTests", ".git", "__pycache__", ".vscode", ".claude"}
SKIP_FILES = {".gitignore", ".gitattributes", "CLAUDE.md"}


def read_version():
    with open(os.path.join(ROOT, "__init__.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "bl_info" for t in node.targets):
            info = ast.literal_eval(node.value)
            return ".".join(str(v) for v in info["version"])
    raise SystemExit("bl_info not found in __init__.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "dist"))
    opts = parser.parse_args()

    version = read_version()
    os.makedirs(opts.out, exist_ok=True)
    target = os.path.join(opts.out, f"{PACKAGE}-{version}.zip")
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if name in SKIP_FILES or name.endswith((".pyc", ".zip", ".blend1")):
                    continue
                path = os.path.join(folder, name)
                arcname = os.path.join(PACKAGE, os.path.relpath(path, ROOT))
                zf.write(path, arcname)
                count += 1
    print(f"{target} ({count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
