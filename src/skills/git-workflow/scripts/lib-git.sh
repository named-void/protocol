# shellcheck shell=bash
# Общие helper-функции локального Git workflow для sync-branch.sh.

_git_helpers_file="$(realpath "${BASH_SOURCE[0]}")"
_git_helpers_dir="${_git_helpers_file%/*}"
_git_remote_parser="$(cd "$_git_helpers_dir/../../.." && pwd -P)/lib/git_remote.py"

fail() {
  local code="$1"
  shift
  echo "$*" >&2
  exit "$code"
}

# Ключ трекера нормализуется в верхний регистр. Ключ выделенной работы — база
# плюс индекс (`UPL-940-6`, `ord-service-1`): регистр наследуется от базы, чтобы
# ключ трекера остался узнаваемым, а производный от каталога не искажался.
normalize_task_key() {
  local key="$1"
  local key_pattern="${AGENTS_KEY_PATTERN:-^[A-Za-z][A-Za-z0-9]*-[0-9]+$}"
  local derived_pattern="${AGENTS_DERIVED_KEY_PATTERN:-^[A-Za-z][A-Za-z0-9]*(-[A-Za-z0-9]+)+-[0-9]+$}"

  if [[ "$key" =~ $key_pattern ]]; then
    tr '[:lower:]' '[:upper:]' <<<"$key"
  elif [[ "$key" =~ $derived_pattern ]]; then
    if [[ "${key%-*}" =~ $key_pattern ]]; then
      tr '[:lower:]' '[:upper:]' <<<"$key"
    else
      printf '%s\n' "$key"
    fi
  else
    return 1
  fi
}

# Ключ выделенной работы, у которой своего ключа в трекере нет.
is_derived_task_key() {
  local key_pattern="${AGENTS_KEY_PATTERN:-^[A-Za-z][A-Za-z0-9]*-[0-9]+$}"
  ! [[ "$1" =~ $key_pattern ]]
}

has_local_branch() {
  git show-ref --verify --quiet "refs/heads/$1"
}

has_origin_branch() {
  # AGENTS_LOCAL_ONLY=1 (режим local-only из protocol): origin-refs не опрашиваются — все
  # origin-проверки схлопываются в локальный путь.
  [[ -z "${AGENTS_LOCAL_ONLY:-}" ]] || return 1
  git show-ref --verify --quiet "refs/remotes/origin/$1"
}

remote_repository_path() {
  python3 "$_git_remote_parser" --field path "$1"
}

is_infrastructure_repository() {
  # shellcheck disable=SC2254  # значение намеренно сопоставляется как glob-маска
  case "$(remote_repository_path "$1")" in
    ${AGENTS_BASE_BRANCH_FALLBACK_GLOB:-*infrastructure/*}) return 0 ;;
  esac
  return 1
}

# Returns success only when both refs have commits absent from the other ref.
branches_diverged() {
  local local_ref="$1"
  local remote_ref="$2"

  ! git merge-base --is-ancestor "$local_ref" "$remote_ref" \
    && ! git merge-base --is-ancestor "$remote_ref" "$local_ref"
}

# Prints the worktree path currently holding the given local branch ref, if
# any registered worktree has it checked out.
worktree_holding_branch() {
  local branch_ref="refs/heads/$1" path=""
  local line
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "branch $branch_ref")
        printf '%s\n' "$path"
        return 0
        ;;
      "") path="" ;;
    esac
  done < <(git worktree list --porcelain)
  return 1
}
