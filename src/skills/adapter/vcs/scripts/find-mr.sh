#!/usr/bin/env bash
# find-mr.sh — определить review-объект (MR) по имени source-ветки, read-only.
#
#   find-mr.sh [branch]
#
# Без аргумента берёт текущую checked-out ветку. Host и project определяются из
# origin, не угадываются и не хардкодятся. Печатает один машиночитаемый исход
# (три случая review-объекта по имени ветки):
#
#   count=0                                  — MR для ветки ещё нет
#   count=1 iid=<IID>                        — ровно один, использовать его
#   count=<N>                                — несколько; далее по строке на MR:
#   iid=<IID> state=<STATE> updated=<TS>       выбор кандидата — за человеком
#
# Скрипт только определяет IID без ссылки; прав это не добавляет. Коды выхода:
#   0  исход определён (любой из трёх)
#   10 не удалось определить ветку
#   11 origin remote не настроен
#   12 запрос к хосту не удался / ответ не JSON-массив

set -euo pipefail

_find_mr_file="$(realpath "${BASH_SOURCE[0]}")"
_find_mr_dir="${_find_mr_file%/*}"
GIT_REMOTE_PARSER="$(cd "$_find_mr_dir/../../../.." && pwd -P)/lib/git_remote.py"

fail() {
  local code="$1"
  shift
  echo "$*" >&2
  exit "$code"
}

BRANCH="${1-}"
if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ -n "$BRANCH" && "$BRANCH" != "HEAD" ]] || fail 10 "conflict: cannot determine current branch; pass it explicitly"
fi

REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
[[ -n "$REMOTE_URL" ]] || fail 11 "conflict: remote 'origin' is not configured"

REMOTE_JSON="$(python3 "$GIT_REMOTE_PARSER" "$REMOTE_URL")"
HOST="$(jq -r '.host // empty' <<<"$REMOTE_JSON")"
PROJECT_PATH="$(jq -r '.repository_path' <<<"$REMOTE_JSON")"
[[ -n "$HOST" ]] || fail 11 "conflict: remote 'origin' has no VCS host"
PROJECT="$(jq -rn --arg value "$PROJECT_PATH" '$value | @uri')"
BRANCH_ENC="$(jq -rn --arg value "$BRANCH" '$value | @uri')"

response="$(glab api --hostname "$HOST" \
  "projects/$PROJECT/merge_requests?source_branch=$BRANCH_ENC&state=all&per_page=20" 2>/dev/null || true)"

# Ответ должен быть JSON-массивом; иначе — сбой доступа/авторизации/пути.
if ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"$response"; then
  fail 12 "conflict: MR lookup on '$HOST' failed or returned non-array (project '$PROJECT_PATH', branch '$BRANCH')"
fi

count="$(jq 'length' <<<"$response")"
if [[ "$count" -eq 0 ]]; then
  echo "count=0"
elif [[ "$count" -eq 1 ]]; then
  echo "count=1 iid=$(jq -r '.[0].iid' <<<"$response")"
else
  echo "count=$count"
  jq -r '.[] | "iid=\(.iid) state=\(.state) updated=\(.updated_at)"' <<<"$response"
fi
