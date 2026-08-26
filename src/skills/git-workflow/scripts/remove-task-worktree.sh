#!/usr/bin/env bash
# Retire one task worktree without losing the evidence that lives only inside it.
# Copying the required non-Git files and removing the tree are one operation here:
# separated, the copy step lags behind the removal and the recorded SHA-256 has
# nothing left to verify.
#
# The branch is never touched: `git worktree remove` drops the working copy, while
# `refs/heads/<type>/<KEY>` keeps the result until a human deletes it.
#
# Usage:
#   remove-task-worktree.sh list
#   remove-task-worktree.sh remove <KEY> <archive-dir> [<sha256>:<path-in-worktree> ...]
#
# Exit codes:
#   0  success; stdout is archived:/removed: lines
#   1  not a git repository
#   10 task worktree is dirty
#   11 task branch has a stash entry
#   22 no registered task worktree for the key
#   24 worktree removal failed
#   25 the script runs inside the worktree it is asked to remove
#   26 the worktree has a detached HEAD
#   30 evidence file is absent in the worktree
#   31 archived copy does not match the recorded SHA-256
#   64 invalid arguments, task key or archive directory

set -euo pipefail

_git_remove_self="${BASH_SOURCE[0]}"
[[ "$_git_remove_self" = /* ]] || _git_remove_self="$PWD/$_git_remove_self"
_git_remove_link="$(readlink "$_git_remove_self" 2>/dev/null || true)"
if [[ -n "$_git_remove_link" ]]; then
  case "$_git_remove_link" in
    /*) _git_remove_self="$_git_remove_link" ;;
    *) _git_remove_self="${_git_remove_self%/*}/$_git_remove_link" ;;
  esac
fi
# shellcheck source=lib-git.sh
source "${_git_remove_self%/*}/lib-git.sh"

usage() {
  echo "usage: remove-task-worktree.sh list" >&2
  echo "       remove-task-worktree.sh remove <KEY> <archive-dir> [<sha256>:<path> ...]" >&2
  exit 64
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    fail 64 "error: neither sha256sum nor shasum is available"
  fi
}

# Prints the local branch checked out in the given worktree, empty for a detached HEAD.
branch_of_worktree() {
  local target="$1" path="" line
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "branch refs/heads/"*)
        [[ "$path" != "$target" ]] || {
          printf '%s\n' "${line#branch refs/heads/}"
          return 0
        }
        ;;
    esac
  done < <(git worktree list --porcelain)
  return 1
}

# A stash entry belongs to the branch named in its own subject: `WIP on <branch>:`
# for the default message, `On <branch>:` for `git stash push -m`.
branch_has_stash() {
  local branch="$1" subject
  while IFS= read -r subject; do
    case "$subject" in
      "WIP on $branch:"* | "On $branch:"*) return 0 ;;
    esac
  done < <(git stash list --format='%gs')
  return 1
}

worktree_is_dirty() {
  [[ -n "$(git -C "$1" status --porcelain)" ]]
}

# Влитость ветки удалению дерева не мешает: дерево — рабочая копия, результат
# держит ветка. Она отвечает на другой вопрос — какую ветку человеку уже можно
# удалить самому, — поэтому печатается в `list` и гейтом не является.
merge_state_of_branch() {
  local branch="$1"
  local base="${AGENTS_BASE_BRANCH:-develop}"
  local base_ref=""

  if has_local_branch "$base"; then
    base_ref="refs/heads/$base"
  elif has_origin_branch "$base"; then
    base_ref="refs/remotes/origin/$base"
  else
    printf 'unknown\n'
    return 0
  fi

  if git merge-base --is-ancestor "refs/heads/$branch" "$base_ref"; then
    printf 'merged\n'
  else
    printf 'unmerged\n'
  fi
}

[[ $# -ge 1 ]] || usage
command="$1"
shift

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fail 1 "error: not a git repository"

git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_repo_root="${git_common_dir%/*}"
main_repo_name="${main_repo_root##*/}"
invoked_root="$(git rev-parse --show-toplevel)"

# Печатает по строке на зарегистрированное task worktree этого репозитория:
# `<KEY> <clean|dirty|stash> <merged|unmerged|unknown> <path>`. Статус задачи
# скрипт не знает — кандидата на уборку выбирает вызывающая сторона по `state.md`.
cmd_list() {
  [[ $# -eq 0 ]] || usage

  local path="" name key state merged branch line
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "") path="" ;;
      *) continue ;;
    esac
    [[ -n "$path" && "$path" != "$main_repo_root" ]] || continue

    name="${path##*/}"
    [[ "$name" == "$main_repo_name-"* ]] || continue
    key="${name#"$main_repo_name"-}"

    branch="$(branch_of_worktree "$path" || true)"
    state=clean
    if worktree_is_dirty "$path"; then
      state=dirty
    elif [[ -n "$branch" ]] && branch_has_stash "$branch"; then
      state=stash
    fi
    if [[ -n "$branch" ]]; then
      merged="$(merge_state_of_branch "$branch")"
    else
      merged=unknown
    fi
    printf '%s %s %s %s\n' "$key" "$state" "$merged" "$path"
  done < <(git worktree list --porcelain)
}

cmd_remove() {
  [[ $# -ge 2 ]] || usage

  local key="$1" archive="$2"
  shift 2

  key="$(normalize_task_key "$key")" \
    || fail 64 "conflict: invalid task key '$key' (expected like ABC-123, ABC-123-1 or repo-1)"

  local worktree_dir="${main_repo_root%/*}/$main_repo_name-$key"
  local branch
  branch="$(branch_of_worktree "$worktree_dir" || true)"
  [[ -d "$worktree_dir" ]] \
    || fail 22 "conflict: '$worktree_dir' is not a registered task worktree"
  # Detached HEAD: коммиты дерева не держит ни одна ветка, и удаление уносит их
  # самих, а не только рабочую копию.
  [[ -n "$branch" ]] \
    || fail 26 "conflict: '$worktree_dir' has a detached HEAD, put its commits on a branch first"

  [[ "$invoked_root" != "$worktree_dir" ]] \
    || fail 25 "conflict: run the removal outside '$worktree_dir'"

  [[ -d "$archive" ]] \
    || fail 64 "conflict: archive directory '$archive' does not exist"
  archive="$(cd "$archive" && pwd -P)"

  # Гейты человека: незакоммиченные изменения и stash своей ветки удаление
  # запрещают всегда — их содержимое не восстанавливается ни веткой, ни копией.
  worktree_is_dirty "$worktree_dir" \
    && fail 10 "conflict: task worktree '$worktree_dir' has uncommitted changes"
  branch_has_stash "$branch" \
    && fail 11 "conflict: branch '$branch' has a stash entry, apply or drop it first"

  local pair expected relative source destination actual
  for pair in "$@"; do
    [[ "$pair" =~ ^[0-9a-fA-F]{64}: ]] \
      || fail 64 "conflict: expected '<sha256>:<path>', got '$pair'"
    expected="$(tr '[:upper:]' '[:lower:]' <<<"${pair%%:*}")"
    relative="${pair#*:}"
    case "$relative" in
      "" | /* | *..*) fail 64 "conflict: '$relative' must be a path inside the worktree" ;;
    esac

    source="$worktree_dir/$relative"
    [[ -f "$source" ]] \
      || fail 30 "conflict: evidence file '$relative' is absent in '$worktree_dir'"

    destination="$archive/$relative"
    mkdir -p "${destination%/*}"
    cp "$source" "$destination"
    actual="$(sha256_of "$destination")"
    if [[ "$actual" != "$expected" ]]; then
      # Копия не подтверждена — в итерации она осталась бы файлом с тем же именем
      # и другим содержимым, то есть ложным доказательством.
      rm -f "$destination"
      fail 31 "conflict: '$relative' is $actual, recorded $expected"
    fi
    echo "archived: $destination"
  done

  local merged
  merged="$(merge_state_of_branch "$branch")"
  git worktree remove "$worktree_dir" \
    || fail 24 "error: cannot remove worktree '$worktree_dir'"
  echo "removed: $branch at $worktree_dir (branch kept, $merged)"
}

case "$command" in
  list) cmd_list "$@" ;;
  remove) cmd_remove "$@" ;;
  *) usage ;;
esac
