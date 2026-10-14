#!/usr/bin/env bash
# Bootstrap навыка protocol: разрешает источник навыка и выводит справочники среды.
# Использование: bash <каталог загруженного protocol>/scripts/bootstrap.sh <имя текущего CLI>
set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo 'usage: bootstrap.sh <имя текущего CLI>' >&2
  exit 2
fi

PROTOCOL_AGENT="$1"
PROTOCOL_SKILL="$(cd "$(dirname "$(realpath "$0")")/.." && pwd -P)"
PROTOCOL_SRC="$(cd "$PROTOCOL_SKILL/../.." && pwd -P)"
PROTOCOL_INSTRUCTIONS="$PROTOCOL_SRC/instructions"

test -r "$PROTOCOL_INSTRUCTIONS/environment.md"
test -r "$PROTOCOL_SRC/lib/skills_config.py"
printf 'PROTOCOL_SKILL=%s\nPROTOCOL_SRC=%s\n' "$PROTOCOL_SKILL" "$PROTOCOL_SRC"
cat "$PROTOCOL_INSTRUCTIONS/environment.md"

PROTOCOL_AGENT_INSTRUCTIONS="$PROTOCOL_INSTRUCTIONS/agent-$PROTOCOL_AGENT.md"
test ! -e "$PROTOCOL_AGENT_INSTRUCTIONS" || cat "$PROTOCOL_AGENT_INSTRUCTIONS"
