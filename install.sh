#!/bin/sh
# Install Pulsar: curl -fsSL https://raw.githubusercontent.com/ronreiter/pulsar/main/install.sh | sh
#
# Installs the `pulsar` command for this user (with uv: an isolated Python 3.12 environment, nothing
# system-wide), then downloads the instruments it plays: Surge XT (an open-source synthesizer) and a
# sampled piano. Run it again to upgrade. Options (environment variables):
#   PULSAR_SOURCE   what to install (default: the GitHub repository's main branch; a local path works)
#   PULSAR_EXTRAS   pip extras (default: voice,mcp -- the singing voice and the MCP server for AI agents)
#   PULSAR_SETUP    "all" also fetches the grand piano and the orchestra (~1.5 GB); "none" skips downloads
set -eu

SOURCE="${PULSAR_SOURCE:-git+https://github.com/ronreiter/pulsar}"
EXTRAS="${PULSAR_EXTRAS:-voice,mcp}"
SETUP="${PULSAR_SETUP:-default}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) warn "Pulsar runs on macOS and Linux; on Windows, use WSL."; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (a Python package manager, into ~/.local/bin)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi

say "Installing pulsar..."
if [ -n "$EXTRAS" ]; then SPEC="pulsar-music[$EXTRAS] @ $SOURCE"; else SPEC="pulsar-music @ $SOURCE"; fi
case "$SOURCE" in
  git+*|http*) ;;
  *) if [ -n "$EXTRAS" ]; then SPEC="$SOURCE[$EXTRAS]"; else SPEC="$SOURCE"; fi ;;
esac
uv tool install --force --python 3.12 "$SPEC"

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
PULSAR="$BIN/pulsar"

if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg is missing: Pulsar needs it for MP3 export and stock sounds."
  if [ "$(uname -s)" = Darwin ]; then warn "  install it with: brew install ffmpeg"; else warn "  install it with your package manager, e.g. sudo apt install ffmpeg"; fi
fi

case "$SETUP" in
  none) ;;
  all) "$PULSAR" setup --all ;;
  *) "$PULSAR" setup ;;
esac

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "Add $BIN to your PATH (or run: uv tool update-shell), then open a new terminal." ;;
esac
say "Done. Try: pulsar docs | less    and    pulsar render song.pulse --mp3 --play"
