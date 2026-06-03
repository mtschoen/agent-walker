#!/usr/bin/env bash
# Build the C++ walker (production impl) and install it as `claude-walker`
# at ~/.local/bin, then register the search MCP server. Smoke test before
# reporting success.
#
# Usage: install.sh [--project [DIR]]
#   (no flag)        register the MCP server at `user` scope (global, every project)
#   --project        register at `local` scope for the directory you invoke from
#   --project DIR    register at `local` scope for DIR
set -euo pipefail

# Capture the invocation directory BEFORE cd-ing into the repo so --project can
# default to "the project the user ran the installer from".
INVOCATION_DIR="$PWD"

MCP_SCOPE="user"
PROJECT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)
            MCP_SCOPE="local"
            # Optional path argument (anything not starting with --).
            if [[ -n "${2:-}" && "${2:-}" != --* ]]; then
                PROJECT_DIR="$2"
                shift
            fi
            ;;
        *)
            echo "install.sh: unknown argument: $1" >&2
            echo "Usage: install.sh [--project [DIR]]" >&2
            exit 2
            ;;
    esac
    shift
done
if [[ "$MCP_SCOPE" == "local" && -z "$PROJECT_DIR" ]]; then
    PROJECT_DIR="$INVOCATION_DIR"
fi

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

# --- Register the search MCP server -----------------------------------------
# Additive: a registration failure warns but does not fail the binary install.
server_path="$SCRIPT_DIR/mcp/server.py"

if ! command -v claude >/dev/null 2>&1; then
    echo
    echo "Note: 'claude' CLI not on PATH; skipped MCP server registration."
    echo "Register later with:"
    echo "  claude mcp add claude-walker -s user -- python \"$server_path\""
else
    # Warn (don't fail) if the MCP SDK isn't importable by the interpreter the
    # server will launch under -- the registration still lands, but the server
    # won't start until `pip install mcp` runs.
    if ! python -c "import mcp.server.fastmcp" >/dev/null 2>&1; then
        echo
        echo "Note: the 'mcp' SDK isn't importable by 'python'. The server is registered"
        echo "but won't start until you run: python -m pip install mcp"
    fi

    if [[ "$MCP_SCOPE" == "local" ]]; then
        if ( cd "$PROJECT_DIR" \
                && { claude mcp remove claude-walker -s local >/dev/null 2>&1 || true; } \
                && claude mcp add claude-walker -s local -- python "$server_path" ); then
            echo "registered claude-walker MCP server (local scope) for $PROJECT_DIR"
        else
            echo "warning: MCP registration (local scope) failed for $PROJECT_DIR" >&2
        fi
    else
        { claude mcp remove claude-walker -s user >/dev/null 2>&1 || true; }
        if claude mcp add claude-walker -s user -- python "$server_path"; then
            echo "registered claude-walker MCP server (user/global scope)"
        else
            echo "warning: MCP registration (user scope) failed" >&2
        fi
    fi
fi
