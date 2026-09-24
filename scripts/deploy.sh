#!/usr/bin/env bash
# Deploy the obfuscated build.
#
# src/ stays the readable source of truth; build/ is generated, is what ships,
# and is gitignored.
#
# Two wrinkles make this more than a copy:
#   * wrangler resolves the Worker's `python_modules/` vendor directory relative
#     to the config file's directory, so the config cannot simply live in
#     build/ -- deploying from there fails with
#     "ModuleNotFoundError: No module named 'workers'".
#   * the entry point must therefore be declared by its path from the project
#     root, not as a bare filename.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== obfuscating src/ -> build/"
python scripts/obfuscate.py src build

echo "== verifying the obfuscated code parses"
python - <<'PY'
import ast, os
for name in sorted(os.listdir("build")):
    if name.endswith(".py"):
        ast.parse(open(os.path.join("build", name), encoding="utf-8").read())
print("   parses OK")
PY

echo "== running the test suite against the obfuscated modules"
PYTHONPATH=build python -m pytest tests/ -q -p no:cacheprovider

echo "== deploying build/ as the entry point"
python - <<'PY'
import io
src = io.open("wrangler.jsonc", encoding="utf-8").read()
out = src.replace('"main": "src/entry.py"', '"main": "build/entry.py"')
assert out != src, "could not find the main entry point to rewrite"
io.open("wrangler.obf.jsonc", "w", encoding="utf-8").write(out)
PY
trap 'rm -f wrangler.obf.jsonc' EXIT

npx --yes wrangler deploy --config wrangler.obf.jsonc "$@"
