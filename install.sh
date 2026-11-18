#!/usr/bin/env bash
# Install agent-bus for one machine. Replace each file atomically so running tools never see a partial copy.
#   mini (default): lib + bin -> ~/.local, `coord` -> coord-mode, coordinator/ -> ~/coord, systemd user units
#   macbook:        forwarders and helpers -> ~/.local/bin, the coord-tabs launchd agent -> ~/Library/LaunchAgents
# Units and launchd agents are copied, never enabled or loaded: see README.md "Install".
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")" && pwd)
dry=0 role=mini
for arg; do
  case $arg in
    --dry-run) dry=1;;
    mini|macbook) role=$arg;;
    *) echo 'usage: install.sh [--dry-run] [mini|macbook]' >&2; exit 2;;
  esac
done
install_file() {
  local src=$1 dest=$2 tmp
  printf '%s -> %s\n' "$src" "$dest"
  [[ $dry == 1 ]] && return 0
  mkdir -p -- "$(dirname -- "$dest")"
  tmp=$(mktemp "${dest}.tmp.XXXXXX")
  if cp -p -- "$src" "$tmp" && mv -f -- "$tmp" "$dest"; then return 0; fi
  rm -f -- "$tmp"
  return 1
}
# Files the user edits after the first install (placeholders, hooks) are never overwritten.
install_once() {
  if [[ -e $2 ]]; then printf '%s -> %s (exists, kept)\n' "$1" "$2"; else install_file "$1" "$2"; fi
}
if [[ $role == macbook ]]; then
  for name in agent-msg agent-task coord coord-tabs coord-watch; do
    install_file "$root/macbook/$name" "$HOME/.local/bin/$name"
  done
  for name in coord-mode coord-statusline; do
    install_file "$root/bin/$name" "$HOME/.local/bin/$name"
  done
  plist=$HOME/Library/LaunchAgents/dev.coord.tabs.plist
  printf '%s -> %s\n' "$root/macbook/launchd/coord-tabs.plist" "$plist"
  if [[ $dry == 0 ]]; then
    mkdir -p -- "${plist%/*}"
    sed "s|@HOME@|$HOME|g" "$root/macbook/launchd/coord-tabs.plist" > "$plist.tmp" && mv -f -- "$plist.tmp" "$plist"
  fi
  exit 0
fi
while IFS= read -r -d '' src; do
  install_file "$src" "$HOME/.local/lib/agentbus/${src#"$root/lib/agentbus/"}"
done < <(find "$root/lib/agentbus" -type f -name '*.py' -print0)
for src in "$root"/bin/*; do
  [[ -f $src ]] && install_file "$src" "$HOME/.local/bin/$(basename -- "$src")"
done
printf '%s -> %s\n' coord-mode "$HOME/.local/bin/coord"
[[ $dry == 1 ]] || ln -sfn coord-mode "$HOME/.local/bin/coord"
for src in "$root"/coordinator/bin/*; do
  install_file "$src" "$HOME/coord/bin/$(basename -- "$src")"
done
install_once "$root/coordinator/CLAUDE.md" "$HOME/coord/CLAUDE.md"
install_once "$root/coordinator/settings.json" "$HOME/coord/.claude/settings.json"
for src in "$root"/systemd/*.service "$root"/systemd/*.timer; do
  install_file "$src" "$HOME/.config/systemd/user/$(basename -- "$src")"
done
