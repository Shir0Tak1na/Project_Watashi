#!/usr/bin/env bash
# Project Watashi launcher (Bash / Git Bash / WSL).
#
# Same reason as run.cmd: do not rely on whatever `python` happens to mean on
# this machine. Resolve the project's own interpreter instead.
#
#   ./run.sh --selftest                run watashi_proto.py --selftest
#   ./run.sh fetch_model --check       run fetch_model.py --check
#   ./run.sh bench_nmt --threads 2,4   run bench_nmt.py --threads 2,4
#
# Works from any working directory.

set -u

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

find_python() {
  # 1. the project venv: POSIX layout, then the Windows layout (Git Bash)
  for candidate in \
    "$HERE/../.venv/bin/python" \
    "$HERE/../.venv/bin/python3" \
    "$HERE/../.venv/Scripts/python.exe" \
    "$HERE/.venv/bin/python" \
    "$HERE/.venv/Scripts/python.exe"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  # 2. an explicit interpreter from the environment
  if [ -n "${WATASHI_PYTHON:-}" ] && command -v "$WATASHI_PYTHON" >/dev/null 2>&1; then
    printf '%s\n' "$WATASHI_PYTHON"
    return 0
  fi
  # 3. plain python3, deliberately not bare `python`
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
    return 0
  fi
  return 1
}

PY="$(find_python)" || {
  echo "[run] No Python interpreter found." >&2
  echo "[run] Expected a virtual environment at:" >&2
  echo "[run]   $HERE/../.venv" >&2
  echo "[run] Create it with:" >&2
  echo "[run]   python3 -m venv \"$HERE/../.venv\"" >&2
  echo "[run]   \"$HERE/../.venv/bin/python\" -m pip install -r \"$HERE/requirements.txt\"" >&2
  exit 2
}

# "$@" preserves the caller's quoting; _run.py decides which script to execute.
exec "$PY" "$HERE/_run.py" "$@"
