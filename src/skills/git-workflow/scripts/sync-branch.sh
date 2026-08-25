#!/usr/bin/env bash
# Create or resume one task branch in its own worktree, so that the repository
# stays usable in parallel. The branch the repository stands on never defines the
# base: a new branch is cut from the project base branch, an existing task branch
# is reused as is.
#
# The only in-place case is a task branch already checked out in the main
# checkout: it is reported as `current:` and kept there. AGENTS_TASK_WORKTREE=always
# splits it out instead, returning the main checkout to the base branch.
#
# Usage: sync-branch.sh <KEY> <branch-type>
#
# Environment:
#   AGENTS_TASK_WORKTREE=always  split the task branch out of the main checkout
#
# Exit codes:
#   0  success; stdout is created:/switched:/current: <branch> ... at <path>
#   1  not a git repository
#   10 task worktree is dirty
#   11 several task branches match the key
#   12 task branch diverged from origin
#   13 base branch diverged from origin
#   15 origin is absent without AGENTS_LOCAL_ONLY=1
#   16 origin fetch failed
#   17 base branch is absent
#   18 branch cannot be updated
#   21 task branch is checked out in another worktree
#   22 expected worktree path is stale
#   23 worktree creation failed
#   64 invalid arguments or task key
#   65 unsupported branch type

set -euo pipefail

_git_sync_self="${BASH_SOURCE[0]}"
[[ "$_git_sync_self" = /* ]] || _git_sync_self="$PWD/$_git_sync_self"
_git_sync_link="$(readlink "$_git_sync_self" 2>/dev/null || true)"
if [[ -n "$_git_sync_link" ]]; then
  case "$_git_sync_link" in
    /*) _git_sync_self="$_git_sync_link" ;;
    *) _git_sync_self="${_git_sync_self%/*}/$_git_sync_link" ;;
  esac
fi
# shellcheck source=lib-git.sh
source "${_git_sync_self%/*}/lib-git.sh"

usage() {
  echo "usage: sync-branch.sh <KEY> <branch-type>" >&2
  exit 64
}

worktree_is_dirty() {
  [[ -n "$(git -C "$1" status --porcelain)" ]]
}

[[ $# -eq 2 ]] || usage
KEY="$1"
TYPE="$2"

KEY="$(normalize_task_key "$KEY")" \
  || fail 64 "conflict: invalid task key '$KEY' (expected like ABC-123, ABC-123-1 or repo-1)"

# shellcheck disable=SC2206
branch_types=(${AGENTS_BRANCH_TYPES:-feature bugfix hotfix})
is_derived_task_key "$KEY" && branch_types+=(protocol)
valid_type=false
for candidate_type in "${branch_types[@]}"; do
  [[ "$TYPE" == "$candidate_type" ]] && valid_type=true && break
done
"$valid_type" || fail 65 "conflict: unknown branch type '$TYPE' (expected ${branch_types[*]})"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || fail 1 "error: not a git repository"

invoked_root="$(git rev-parse --show-toplevel)"
if origin_remote="$(git remote get-url origin 2>/dev/null)"; then
  if [[ -z "${AGENTS_LOCAL_ONLY:-}" ]] && ! git fetch origin --prune --quiet; then
    fail 16 "conflict: cannot fetch remote 'origin'"
  fi
else
  origin_remote=""
  [[ -n "${AGENTS_LOCAL_ONLY:-}" ]] \
    || fail 15 "conflict: remote 'origin' is not configured"
fi

git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_repo_root="$(dirname "$git_common_dir")"
worktree_dir="$(dirname "$main_repo_root")/$(basename "$main_repo_root")-$KEY"

# Задача всегда получает своё дерево: только так по репозиторию можно работать
# параллельно. Ветка, на которой стоит репозиторий, базу не задаёт — новая
# отрезается от базовой ветки проекта, существующая переиспользуется как есть.
base_branch="${AGENTS_BASE_BRANCH:-develop}"
local_base=false
origin_base=false
has_local_branch "$base_branch" && local_base=true
has_origin_branch "$base_branch" && origin_base=true

if ! "$local_base" && ! "$origin_base"; then
  if [[ -n "$origin_remote" ]] && is_infrastructure_repository "$origin_remote"; then
    base_branch="${AGENTS_BASE_BRANCH_FALLBACK:-master}"
    has_local_branch "$base_branch" && local_base=true
    has_origin_branch "$base_branch" && origin_base=true
  fi
fi
base_known=false
{ "$local_base" || "$origin_base"; } && base_known=true

candidates=()
for candidate_type in "${branch_types[@]}"; do
  candidate="$candidate_type/$KEY"
  if has_local_branch "$candidate" || has_origin_branch "$candidate"; then
    candidates+=("$candidate")
  fi
done

if [[ ${#candidates[@]} -gt 1 ]]; then
  echo "conflict: multiple task branches exist for '$KEY', pick one:" >&2
  printf '  %s\n' "${candidates[@]}" >&2
  exit 11
fi

if [[ ${#candidates[@]} -eq 1 ]]; then
  branch="${candidates[0]}"
  local_exists=false
  origin_exists=false
  has_local_branch "$branch" && local_exists=true
  has_origin_branch "$branch" && origin_exists=true

  if "$local_exists" && "$origin_exists" \
    && branches_diverged "refs/heads/$branch" "refs/remotes/origin/$branch"; then
    fail 12 "conflict: '$branch' diverged from origin, merge it before continuing"
  fi

  holder=""
  "$local_exists" && holder="$(worktree_holding_branch "$branch" || true)"
  if [[ -n "$holder" ]]; then
    if [[ "$holder" != "$worktree_dir" && "$holder" != "$invoked_root" ]]; then
      fail 21 "conflict: task branch '$branch' is already checked out at '$holder'"
    fi
    worktree_is_dirty "$holder" \
      && fail 10 "conflict: task worktree '$holder' has uncommitted changes"
    if "$origin_exists" \
      && git merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch" \
      && [[ "$(git rev-parse "refs/heads/$branch")" != "$(git rev-parse "refs/remotes/origin/$branch")" ]]; then
      git -C "$holder" merge --ff-only --quiet "origin/$branch" \
        || fail 18 "error: cannot fast-forward '$branch' from origin"
    fi

    if [[ "$holder" == "$main_repo_root" && "${AGENTS_TASK_WORKTREE:-}" == "always" ]]; then
      "$base_known" \
        || fail 17 "conflict: base branch '${AGENTS_BASE_BRANCH:-develop}' is absent locally and on origin"
      [[ ! -e "$worktree_dir" ]] \
        || fail 22 "conflict: path '$worktree_dir' exists but is not the worktree for '$branch'"
      git -C "$main_repo_root" switch --quiet "$base_branch" \
        || fail 18 "error: cannot free main checkout '$main_repo_root' from '$branch'"
      git worktree add --quiet "$worktree_dir" "$branch" \
        || fail 23 "error: cannot create worktree '$worktree_dir' for '$branch'"
      echo "switched: $branch at $worktree_dir"
      exit 0
    fi

    echo "current: $branch at $holder"
    exit 0
  fi

  if "$local_exists" && "$origin_exists" \
    && git merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch"; then
    git branch -f "$branch" "origin/$branch" >/dev/null 2>&1 \
      || fail 18 "error: cannot fast-forward '$branch' from origin"
  elif ! "$local_exists"; then
    git branch --no-track "$branch" "origin/$branch" >/dev/null 2>&1 \
      || fail 18 "error: cannot create '$branch' from origin"
  fi

  [[ ! -e "$worktree_dir" ]] \
    || fail 22 "conflict: path '$worktree_dir' exists but is not the worktree for '$branch'"

  git worktree add --quiet "$worktree_dir" "$branch" \
    || fail 23 "error: cannot create worktree '$worktree_dir' for '$branch'"
  echo "switched: $branch at $worktree_dir"
  exit 0
fi

if ! "$base_known"; then
  fail 17 "conflict: base branch '${AGENTS_BASE_BRANCH:-develop}' is absent locally and on origin"
fi

if "$local_base" && "$origin_base" \
  && branches_diverged "refs/heads/$base_branch" "refs/remotes/origin/$base_branch"; then
  fail 13 "conflict: '$base_branch' diverged from origin, resolve it before creating the task branch"
fi

if "$origin_base" \
  && { ! "$local_base" || git merge-base --is-ancestor "refs/heads/$base_branch" "refs/remotes/origin/$base_branch"; }; then
  base_ref="origin/$base_branch"
else
  base_ref="$base_branch"
fi

if [[ -n "${AGENTS_LOCAL_ONLY:-}" || -z "$origin_remote" ]]; then
  echo "warning: base '$base_branch' resolved without origin sync; it may lag behind upstream" >&2
fi

branch="$TYPE/$KEY"

[[ ! -e "$worktree_dir" ]] \
  || fail 22 "conflict: path '$worktree_dir' exists but is not a registered task worktree"

git worktree add --quiet --no-track -b "$branch" "$worktree_dir" "$base_ref" \
  || fail 23 "error: cannot create worktree '$worktree_dir' for '$branch'"
echo "created: $branch (from $base_branch) at $worktree_dir"
