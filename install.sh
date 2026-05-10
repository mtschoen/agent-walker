#!/usr/bin/env bash
# Build the C++ walker (production impl) and install it as `claude-walker`
# at ~/.local/bin. Smoke test before reporting success.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build --config Release -j

# Locate the binary across single-config (Make/Ninja: build/walker) and
# multi-config (VS: build/Release/walker.exe) generators. Check .exe
# variants first because git-bash on Windows resolves `[[ -f "walker" ]]`
# true for an existing `walker.exe`, which would land us on the wrong
# candidate string and skip the case-based suffix below.
walker_bin=""
for candidate in cpp/build/Release/walker.exe cpp/build/walker.exe cpp/build/Release/walker cpp/build/walker; do
    if [[ -f "$candidate" ]]; then
        walker_bin="$candidate"
        break
    fi
done
if [[ -z "$walker_bin" ]]; then
    echo "install.sh: built walker binary not found under cpp/build/" >&2
    exit 1
fi

mkdir -p "$HOME/.local/bin"
target="$HOME/.local/bin/claude-walker"
case "$walker_bin" in
    *.exe) target="$target.exe" ;;
esac
cp "$walker_bin" "$target"
echo "installed $walker_bin -> $target"

# Smoke test: bare-flag invocation routes to cost mode.
if "$target" --period 86400 --win-start 0 >/dev/null; then
    echo "smoke test ok"
else
    echo "smoke test FAILED" >&2
    exit 1
fi

if ! command -v claude-walker >/dev/null 2>&1; then
    echo
    echo "Note: $HOME/.local/bin is not on PATH. Add it before the recency-nudge"
    echo "hook or status line can find claude-walker by name."
fi
