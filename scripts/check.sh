#!/bin/sh
# Full verification, in the order that fails fastest and most informatively.
#
# The syntax pass comes first on purpose. mypy and ruff report one error at a time, so a
# stray bracket in a function body shows up as an unrelated type error several steps later,
# after the obvious cause has scrolled away.
set -e

cd "$(dirname "$0")/.."

echo "==> syntax"
# Uses the project interpreter, not the system python3: this project targets 3.14 and uses
# PEP 758 parenthesless except clauses, which older interpreters reject.
find src tests -name '*.py' -print0 | xargs -0 -n1 uv run python -c '
import ast, sys, pathlib
path = pathlib.Path(sys.argv[1])
try:
    ast.parse(path.read_text())
except SyntaxError as exc:
    sys.exit(f"{path}:{exc.lineno}: {exc.msg}")
'

echo "==> ruff check"
uv run ruff check .

echo "==> ruff format"
uv run ruff format --check .

echo "==> mypy"
uv run mypy

echo "==> pytest"
uv run pytest -q

echo "==> leaked daemons"
if pgrep -f "code_review daemon run" >/dev/null 2>&1; then
    echo "FAIL: a code-review daemon is still running" >&2
    pgrep -fl "code_review daemon run" >&2
    exit 1
fi
echo "none"

echo
echo "all checks passed"