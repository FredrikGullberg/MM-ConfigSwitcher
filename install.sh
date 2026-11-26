#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -v
if [[ ! -f "$HOME/.config/mm-configswitcher/profiles.json" && ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/mm-configswitcher/profiles.json" ]]; then
    python3 switcher.py init "$@"
elif (( $# > 0 )); then
    echo "Already initialized. Edit profiles.json deliberately; init options are not reapplied." >&2
    exit 1
fi
python3 switcher.py install
mkdir -p "$HOME/.local/bin"
target="$HOME/.local/bin/mm-switch"
if [[ ! -e "$target" && ! -L "$target" ]]; then
    ln -s "$(pwd -P)/mm-switch" "$target"
fi
echo "Use $HOME/.local/bin/mm-switch status"
