#!/usr/bin/env bash
# Report which requested CI toolchains the fleet Linux runner image already
# carries at its manifest pin, so a workflow can skip the step that would
# download them (schoen-lab#3353).
#
# Usage: ci/baked-toolchain.sh TOOL... where each TOOL is one of uv, node,
# python, shellcheck, markdownlint_cli2, kcov, php, dotnet, go, rust and zig.
# It writes
# TOOL=true|false to $GITHUB_OUTPUT. A baked
# node, or a virtual environment over a baked python, is also prepended to
# $GITHUB_PATH, as actions/setup-node and actions/setup-python would do.
# Outside the fleet image (no image.env, or an image built before a tool was
# baked) every tool reports false and the
# workflow's own setup step runs as before. A probe counts only when its
# command exits zero and prints the pinned version (markdownlint_cli2 is the
# exception: it exits 2 without files, so only its banner counts). uv reports false in a
# checkout without packages/local_ci/runner/manifest.toml, its one registry.
set -euo pipefail

image_env=${FLEET_IMAGE_ENV:-/usr/local/share/fleet-runner/image.env}
: "${GITHUB_OUTPUT:?}" "${GITHUB_PATH:?}"

if [[ -f "$image_env" ]]; then
  # shellcheck source=/dev/null
  source "$image_env"
fi

# The uv version this checkout requires: the runner manifest is the one
# registry, so a baked uv older than the branch's pin must not skip setup-uv.
required_uv_version() {
  local manifest
  manifest="$(dirname "${BASH_SOURCE[0]}")/../packages/local_ci/runner/manifest.toml"
  [[ -f "$manifest" ]] || return 0
  awk '/^\[/ { in_uv = ($0 == "[tools.uv]") } in_uv && /^version[[:space:]]*=/ { gsub(/[" ]/, "", $3); print $3; exit }' "$manifest"
}

version_output() {
  local output
  output="$("$@" 2> /dev/null)" || return 1
  printf '%s\n' "$output"
}

baked_rust() {
  local rust_home="/opt/fleet/rust-${FLEET_RUST_VERSION:-}"
  local toolchain="${FLEET_RUST_VERSION:-}-x86_64-unknown-linux-gnu"
  local rust_bin="$rust_home/rustup/toolchains/$toolchain/bin"
  local cargo_home="${CARGO_HOME:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/baked-cargo}"
  local version
  [[ -n "${FLEET_RUST_VERSION:-}" ]] \
    && version="$(version_output "$rust_bin/rustc" --version)" \
    && [[ "$version" == "rustc $FLEET_RUST_VERSION "* ]] \
    && version="$(version_output "$rust_bin/cargo" --version)" \
    && [[ "$version" == "cargo ${FLEET_RUST_VERSION%.*}."* ]] \
    && mkdir -p "$cargo_home/bin" \
    && [[ -w "$cargo_home" ]] \
    && printf '%s\n' "$cargo_home/bin" "$rust_bin" "$rust_home/cargo/bin" >> "$GITHUB_PATH" \
    && printf '%s\n' "RUSTUP_HOME=$rust_home/rustup" "CARGO_HOME=$cargo_home" \
      "RUSTUP_TOOLCHAIN=$toolchain" "RUSTUP_AUTO_INSTALL=0" >> "${GITHUB_ENV:?}"
}

baked() {
  local version
  case "$1" in
    uv)
      local required_uv
      required_uv="$(required_uv_version)"
      [[ -n "${FLEET_UV_VERSION:-}" && -n "$required_uv" ]] \
        && [[ "$FLEET_UV_VERSION" == "$required_uv" ]] \
        && version="$(version_output uv --version)" \
        && [[ "$version" == "uv $required_uv"* ]]
      ;;
    node)
      local node_bin="/opt/fleet/node-${FLEET_NODE_VERSION:-}/bin"
      [[ -n "${FLEET_NODE_VERSION:-}" ]] \
        && version="$(version_output "$node_bin/node" --version)" \
        && [[ "$version" == "v$FLEET_NODE_VERSION" ]] \
        && printf '%s\n' "$node_bin" >> "$GITHUB_PATH"
      ;;
    python)
      local python_bin="/opt/fleet/python-${FLEET_PYTHON_VERSION:-}/bin"
      # The baked interpreter is uv-installed and PEP 668 marked, so pip refuses
      # to install into it; a virtual environment over it is the writable
      # interpreter actions/setup-python would provide.
      local python_venv="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/baked-python"
      [[ -n "${FLEET_PYTHON_VERSION:-}" ]] \
        && version="$(version_output "$python_bin/python" --version)" \
        && [[ "$version" == "Python $FLEET_PYTHON_VERSION" ]] \
        && "$python_bin/python" -m venv "$python_venv" \
        && printf '%s\n' "$python_venv/bin" >> "$GITHUB_PATH"
      ;;
    shellcheck)
      [[ -n "${SHELLCHECK_MAJOR:-}" ]] \
        && version="$(version_output shellcheck --version)" \
        && [[ "$version" == *"version: $SHELLCHECK_MAJOR."* ]]
      ;;
    markdownlint_cli2)
      # `markdownlint-cli2 --help` prints the banner but exits 2, so the
      # banner on its first line is the whole probe.
      [[ -n "${MARKDOWNLINT_CLI2_VERSION:-}" ]] \
        && command -v markdownlint-cli2 > /dev/null \
        && version="$(markdownlint-cli2 --help 2>&1 | head -n 1 || true)" \
        && [[ "$version" == "markdownlint-cli2 v$MARKDOWNLINT_CLI2_VERSION "* ]]
      ;;
    kcov)
      # A tarball build of the pinned master commit has no .git, so
      # `kcov --version` reports the ChangeLog version, not the commit;
      # FLEET_KCOV_REPORTED carries that expectation from the manifest.
      local kcov_bin="/opt/fleet/kcov-${FLEET_KCOV_VERSION:-}/bin"
      [[ -n "${FLEET_KCOV_VERSION:-}" && -n "${FLEET_KCOV_REPORTED:-}" ]] \
        && version="$(version_output "$kcov_bin/kcov" --version)" \
        && [[ "$version" == "$FLEET_KCOV_REPORTED" ]] \
        && printf '%s\n' "$kcov_bin" >> "$GITHUB_PATH"
      ;;
    php)
      [[ -n "${FLEET_PHP_VERSION:-}" ]] \
        && version="$(version_output php --version)" \
        && [[ "$version" == "PHP $FLEET_PHP_VERSION."* ]]
      ;;
    dotnet)
      # Apphosts (and tools such as dotnet-format launchers) need DOTNET_ROOT to
      # find the baked runtime; the install is the directory the dotnet link
      # resolves into.
      local dotnet_root
      [[ -n "${FLEET_DOTNET_VERSION:-}" ]] \
        && version="$(version_output dotnet --version)" \
        && [[ "$version" == "$FLEET_DOTNET_VERSION" ]] \
        && dotnet_root="$(dirname "$(readlink -f "$(command -v dotnet)")")" \
        && printf '%s\n' "DOTNET_ROOT=$dotnet_root" >> "${GITHUB_ENV:?}"
      ;;
    go)
      local go_bin="/opt/fleet/go-${FLEET_GO_VERSION:-}/bin"
      [[ -n "${FLEET_GO_VERSION:-}" ]] \
        && version="$(version_output "$go_bin/go" version)" \
        && [[ "$version" == "go version go$FLEET_GO_VERSION linux/amd64" ]] \
        && printf '%s\n' "$go_bin" >> "$GITHUB_PATH"
      ;;
    rust)
      baked_rust
      ;;
    zig)
      local zig_bin="/opt/fleet/zig-${FLEET_ZIG_VERSION:-}"
      [[ -n "${FLEET_ZIG_VERSION:-}" ]] \
        && version="$(version_output "$zig_bin/zig" version)" \
        && [[ "$version" == "$FLEET_ZIG_VERSION" ]] \
        && printf '%s\n' "$zig_bin" >> "$GITHUB_PATH"
      ;;
    *)
      printf 'unknown toolchain: %s\n' "$1" >&2
      exit 2
      ;;
  esac
}

if [[ $# -eq 0 ]]; then
  printf 'usage: %s TOOL...\n' "$0" >&2
  exit 2
fi
for tool in "$@"; do
  if baked "$tool"; then
    state=true
  else
    state=false
  fi
  printf '%s=%s\n' "$tool" "$state" >> "$GITHUB_OUTPUT"
  printf 'baked %s: %s\n' "$tool" "$state"
done
