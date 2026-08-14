#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'usage: %s <install|uninstall> <link-path> <source-path>\n' "$0" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage

mode=$1
link=$2
source=$3

case "$mode" in
  install)
    if [[ -e "$link" && ! -L "$link" ]]; then
      printf 'refusing to replace non-symlink instructions file: %s\n' "$link" >&2
      exit 1
    fi
    if [[ -L "$link" ]]; then
      target=$(readlink "$link")
      if [[ "$target" != "$source" && -e "$link" ]]; then
        printf 'refusing to replace foreign instructions symlink: %s -> %s\n' "$link" "$target" >&2
        exit 1
      fi
    fi
    mkdir -p "$(dirname "$link")"
    ln -sfn "$source" "$link"
    ;;
  uninstall)
    if [[ -L "$link" && $(readlink "$link") == "$source" ]]; then
      rm -f "$link"
    fi
    ;;
  *) usage ;;
esac
