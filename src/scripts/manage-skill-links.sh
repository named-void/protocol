#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'usage: %s <install|uninstall> <destination-root> <link-name> <source-path> [<link-name> <source-path> ...]\n' "$0" >&2
  exit 2
}

[[ $# -ge 4 && $(( ($# - 2) % 2 )) -eq 0 ]] || usage

mode=$1
destination_root=${2%/}
shift 2

replace_matching_directory() {
  local source=$1
  local destination=$2
  local file relative

  while IFS= read -r -d '' file; do
    relative=${file#"$destination"/}
    if [[ ! -f "$source/$relative" ]] || ! cmp -s "$file" "$source/$relative"; then
      printf 'refusing to replace non-matching skill directory: %s\n' "$destination" >&2
      exit 1
    fi
  done < <(find "$destination" -type f -print0)

  if find "$destination" \( -type l -o \( ! -type d ! -type f \) \) -print -quit | grep -q .; then
    printf 'refusing to replace skill directory with non-regular entries: %s\n' "$destination" >&2
    exit 1
  fi

  rm -rf "$destination"
}

install_skill() {
  local link_name=$1
  local source=$2
  local destination="$destination_root/$link_name"

  [[ -d "$source" ]] || {
    printf 'skill source not found: %s\n' "$source" >&2
    exit 1
  }

  if [[ -L "$destination" ]]; then
    local current
    current=$(readlink "$destination")
    if [[ "$current" != "$source" && -e "$destination" ]]; then
      printf 'refusing to replace foreign skill symlink: %s -> %s\n' "$destination" "$current" >&2
      exit 1
    fi
  elif [[ -e "$destination" ]]; then
    [[ -d "$destination" ]] || {
      printf 'refusing to replace non-directory skill path: %s\n' "$destination" >&2
      exit 1
    }
    replace_matching_directory "$source" "$destination"
  fi

  ln -sfn "$source" "$destination"
}

uninstall_skill() {
  local link_name=$1
  local source=$2
  local destination="$destination_root/$link_name"

  [[ -L "$destination" ]] || return 0
  if [[ $(readlink "$destination") != "$source" ]]; then
    printf 'refusing to remove unmanaged skill symlink: %s\n' "$destination" >&2
    exit 1
  fi
  rm -f "$destination"
}

mkdir -p "$destination_root"

while [[ $# -gt 0 ]]; do
  case "$mode" in
    install) install_skill "$1" "$2" ;;
    uninstall) uninstall_skill "$1" "$2" ;;
    *) usage ;;
  esac
  shift 2
done
