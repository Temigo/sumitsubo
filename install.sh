#!/bin/sh
# Sumitsubo: install in one command, from the folder this file is in.
#
#     sh install.sh              everything
#     sh install.sh --no-paper   without the reader for photographed pages (a smaller download)
#
# It makes a private Python environment in .venv, installs the program into it, puts a `sumitsubo`
# command in ~/.local/bin, and downloads the dictionary data. Nothing is installed system-wide and
# nothing needs sudo. Running it again updates an existing install. Linux and macOS.
set -eu
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

PY=""
for p in python3.13 python3.12 python3.11 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$p"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.11 or newer is needed (Debian and Ubuntu: sudo apt install python3 python3-venv; macOS: brew install python)."
command -v git >/dev/null 2>&1 || fail "git is needed (sudo apt install git; macOS: xcode-select --install)."
command -v ffmpeg >/dev/null 2>&1 || say "Note: ffmpeg was not found. Everything works without it except audio and video sources (sudo apt install ffmpeg; macOS: brew install ffmpeg)."

say "1 of 4: a private Python environment in .venv ($("$PY" --version))"
[ -x .venv/bin/python ] || "$PY" -m venv .venv || fail "could not make the environment (Debian and Ubuntu: sudo apt install python3-venv)."

say "2 of 4: the program and its libraries (several minutes the first time)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .

say "3 of 4: the sumitsubo command"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$(pwd)/.venv/bin/sumitsubo" "$BIN/sumitsubo"

say "4 of 4: dictionary data"
.venv/bin/sumitsubo setup "$@"

say ""
say "Done. Start it with:   sumitsubo serve     then open http://127.0.0.1:8378"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "(~/.local/bin is not on your PATH in this shell. Open a new terminal, or run $BIN/sumitsubo serve)";;
esac
