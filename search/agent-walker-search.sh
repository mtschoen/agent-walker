#!/usr/bin/env bash
# Git Bash launcher for agent-walker-search on Windows. Git Bash does not run a
# `.bat` under its extensionless name, so install.bat copies this beside the
# `.bat` as `agent-walker-search`. Interpreter choice mirrors the `.bat`:
# `py -3` when it is Python 3.10 or newer, otherwise `python`.
script="$(dirname "$0")/agent-walker-search.py"
if command -v py >/dev/null 2>&1 \
    && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
    exec py -3 "$script" "$@"
fi
exec python "$script" "$@"
