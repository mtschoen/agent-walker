#!/usr/bin/env bash
# Install the native walker, its claude-walker alias, and the stdlib-only
# session-search CLI at ~/.local/bin. Smoke test before reporting success.
#
# Usage: install.sh
set -euo pipefail

if [[ $# -gt 0 ]]; then
    echo "install.sh: unknown argument: $1" >&2
    echo "Usage: install.sh" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Build whichever impl this host has a toolchain for. C++ is the production
# binary and is tried first; Go and Rust are conformance-equal fallbacks for
# hosts without a C++ toolchain (e.g. SteamOS's immutable root, where there is
# no cmake/cc at all but a user-local `go` works). Each builder writes its tool
# output to stderr and prints ONLY the resulting binary path to stdout, so the
# caller can capture the path via command substitution; a non-zero return means
# "toolchain absent or build failed, try the next one".
build_cpp() {
    command -v cmake >/dev/null 2>&1 || return 1
    cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release >&2 || return 1
    cmake --build cpp/build --config Release -j >&2 || return 1
    # Check .exe variants first because git-bash on Windows resolves
    # `[[ -f "walker" ]]` true for an existing `walker.exe`, which would land us
    # on the wrong candidate string and skip the case-based suffix below.
    local candidate
    for candidate in cpp/build/Release/walker.exe cpp/build/walker.exe cpp/build/Release/walker cpp/build/walker; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

build_go() {
    command -v go >/dev/null 2>&1 || return 1
    # CGO off so the pure-Go linker is used -- no system `cc` required.
    ( cd go && CGO_ENABLED=0 go build -o walker . ) >&2 || return 1
    [[ -f go/walker ]] && { printf '%s\n' "go/walker"; return 0; }
    return 1
}

build_rust() {
    command -v cargo >/dev/null 2>&1 || return 1
    # rustc shells out to a system linker; skip if there is no C compiler.
    command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 \
        || command -v clang >/dev/null 2>&1 || return 1
    ( cd rust && cargo build --release ) >&2 || return 1
    [[ -f rust/target/release/walker ]] && {
        printf '%s\n' "rust/target/release/walker"
        return 0
    }
    return 1
}

walker_bin=""
for builder in build_cpp build_go build_rust; do
    if walker_bin="$("$builder")"; then
        echo "built walker via ${builder#build_}: $walker_bin" >&2
        break
    fi
done
if [[ -z "$walker_bin" ]]; then
    echo "install.sh: no usable toolchain to build the walker." >&2
    echo "            install one of: cmake + a C++ compiler, or go, or cargo + cc." >&2
    exit 1
fi

mkdir -p "$HOME/.local/bin"
target="$HOME/.local/bin/agent-walker"
alias_target="$HOME/.local/bin/claude-walker"
case "$walker_bin" in
    *.exe) target="$target.exe"; alias_target="$alias_target.exe" ;;
esac
cp "$walker_bin" "$target"
# claude-walker is a deprecated back-compat alias for existing callers
# (progress-beacon hooks, statusline lookups). Remove once every consumer
# has migrated to the agent-walker name.
cp "$walker_bin" "$alias_target"
echo "installed $walker_bin -> $target (alias: $alias_target)"

# Smoke test: bare-flag invocation routes to cost mode.
if "$target" --period 86400 --win-start 0 >/dev/null; then
    echo "smoke test ok"
else
    echo "smoke test FAILED" >&2
    exit 1
fi

# The session-search CLI needs Python 3.10+. Hosts expose it as python3,
# python, or only a versioned python3.N, so probe each candidate and bake the
# first usable one into the installed launcher.
find_python() {
    local candidate
    for candidate in python3 python \
            python3.20 python3.19 python3.18 python3.17 python3.16 python3.15 \
            python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 \
                && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
                    >/dev/null 2>&1; then
            absolute_path "$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

# command -v can answer with a PATH-relative name; the launcher runs from any
# directory, so persist an absolute path.
absolute_path() {
    local directory
    directory="$(cd "$(dirname "$1")" && pwd -P)" || return 1
    printf '%s/%s\n' "$directory" "$(basename "$1")"
}

# Quote a value as one literal POSIX-shell word for the generated launcher, so
# metacharacters in HOME or an interpreter directory are never executed.
shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# write_search_launcher <launcher> <python> <script>
write_search_launcher() {
    printf '#!/bin/sh\nexec %s %s "$@"\n' "$(shell_quote "$2")" "$(shell_quote "$3")" > "$1"
    chmod +x "$1"
}

if ! python_bin="$(find_python)"; then
    echo "install.sh: no Python 3.10+ found (tried python3, python, python3.N)." >&2
    exit 1
fi

search_target="$HOME/.local/bin/agent-walker-search"
search_script="$HOME/.local/bin/agent-walker-search.py"
cp "$SCRIPT_DIR/search/session_search.py" "$search_script"
cp "$SCRIPT_DIR/search/opencode_search.py" "$HOME/.local/bin/opencode_search.py"
cp "$SCRIPT_DIR/shared/process_safe.py" "$HOME/.local/bin/process_safe.py"
write_search_launcher "$search_target" "$python_bin" "$search_script"
echo "installed session-search CLI -> $search_target (python: $python_bin)"

if "$search_target" --help >/dev/null; then
    echo "session-search smoke test ok"
else
    echo "session-search smoke test FAILED" >&2
    exit 1
fi

if ! command -v agent-walker >/dev/null 2>&1 \
        || ! command -v agent-walker-search >/dev/null 2>&1; then
    echo
    echo "Note: $HOME/.local/bin is not on PATH. Add it before the recency-nudge"
    echo "hook, status line, or session-search skill can find their commands by name."
fi
