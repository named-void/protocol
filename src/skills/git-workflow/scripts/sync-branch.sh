#!/usr/bin/env bash
# Sync a task key to its own git worktree, so parallel agents/sessions
# never write into the same checkout. See ../SKILL.md for the decision
# rules (branch naming, when to ask the user instead of guessing).
#
# Usage: sync-branch.sh <KEY> <branch-type> [label]
#
#   label ([a-z0-9-]+), when omitted, is taken from $AGENTS_EXECUTOR (set
#   for dispatched sessions by launch-session.sh), else auto-detected from
#   this script's own resolved install path (.codex/.claude/.kilo/.cursor).
#   Pass it explicitly only as the fallback the caller uses after asking
#   the user once which executor the current session is.
#
# Worktree layout: a sibling directory of the main repository root named
# "<repo>-<KEY>-<label>" holds a linked git worktree checked out to the
# local integration branch "<type>/<KEY>-<label>". The canonical task
# branch remains "<type>/<KEY>" and is merged only after user acceptance by
# accept-worktree.sh. The main checkout itself is never switched or required
# to be clean.
#
# Exit codes:
#   0  success — stdout names the canonical branch, agent branch and path:
#      "current: <canonical> via <agent-branch> at <path>" / "switched:
#      <canonical> (existing) via <agent-branch> at <path>" / "created:
#      <canonical> (from <base-branch>) via <agent-branch> at <path>"
#   1  not a git repository
#   10 dirty worktree — the task's own worktree has uncommitted changes to
#      tracked files (checked only when resuming an existing worktree);
#      untracked/ignored files are intentionally ignored
#   11 ambiguous — several canonical branches exist for KEY
#   12 existing branch diverged from origin
#   13 selected base branch diverged from origin
#   14 target branch appeared while creating it
#   15 origin remote is not configured (not required when AGENTS_LOCAL_ONLY=1)
#   16 cannot fetch origin
#   17 no permitted base branch is available
#   18 git could not fast-forward the worktree or local branch
#   19 cannot auto-detect label and none was passed explicitly
#   20 explicit label argument is not [a-z0-9-]+
#   21 target canonical/agent branch is already checked out in another worktree
#   22 the task's worktree path exists but is not a registered git worktree
#      for this branch
#   23 git could not create the worktree
#   64 usage error / invalid issue key
#   65 unknown branch type
#
# Expected conflicts are checked before any worktree is created or touched.
# The caller (protocol's Branch-Sync) must stop on every non-zero
# exit instead of retrying blindly.

set -euo pipefail

# lib-git.sh лежит рядом с РЕАЛЬНЫМ файлом скрипта: симлинк одиночного файла
# резолвим (SCRIPT_INVOKED_PATH ниже, наоборот, намеренно оставлен как вызван —
# нерезолвленный путь несёт сигнал label). Симлинк каталога навыка прозрачен.
_vcs_self="${BASH_SOURCE[0]}"
[[ "$_vcs_self" = /* ]] || _vcs_self="$PWD/$_vcs_self"
_vcs_link="$(readlink "$_vcs_self" 2>/dev/null || true)"
if [[ -n "$_vcs_link" ]]; then
  case "$_vcs_link" in
    /*) _vcs_self="$_vcs_link" ;;
    *) _vcs_self="${_vcs_self%/*}/$_vcs_link" ;;
  esac
fi
# shellcheck source=lib-git.sh
source "${_vcs_self%/*}/lib-git.sh"

usage() {
  echo "usage: sync-branch.sh <KEY> <branch-type> [label]" >&2
  exit 64
}

worktree_is_dirty() {
  [[ -n "$(git -C "$1" status --porcelain --untracked-files=no)" ]]
}

[[ $# -eq 2 || $# -eq 3 ]] || usage
KEY="$1"
TYPE="$2"
LABEL="${3-}"

KEY="$(normalize_task_key "$KEY")" \
  || fail 64 "conflict: invalid task key '$KEY' (expected like ABC-123, ABC-123-2, or common-1)"
if [[ "$KEY" =~ ^common-[0-9]+$ ]]; then
  [[ "$TYPE" == "protocol" ]] \
    || fail 65 "conflict: common task '$KEY' requires branch type 'protocol'"
fi

# shellcheck disable=SC2206  # word-splitting намеренно формирует массив
branch_types=(${AGENTS_BRANCH_TYPES:-feature bugfix hotfix})
[[ "$KEY" =~ ^common-[0-9]+$ ]] && branch_types+=(protocol)
valid_type=false
for t in "${branch_types[@]}"; do
  [[ "$TYPE" == "$t" ]] && valid_type=true && break
done
"$valid_type" || fail 65 "conflict: unknown branch type '$TYPE' (expected ${branch_types[*]})"

# Deliberately NOT symlink-resolved: each CLI's skills are exposed through
# its own symlink (~/.claude/skills/git-workflow -> this repo, ~/.codex/skills/git-workflow
# -> this repo, ...), and that symlink segment is exactly the label
# signal. Resolving it away would always yield this repo's real path.
SCRIPT_INVOKED_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_INVOKED_PATH" in
  /*) ;;
  *) SCRIPT_INVOKED_PATH="$PWD/$SCRIPT_INVOKED_PATH" ;;
esac
SCRIPT_INVOKED_DIR="$(dirname -- "$SCRIPT_INVOKED_PATH")"

if [[ -n "$LABEL" ]]; then
  if [[ ! "$LABEL" =~ ^[a-z0-9-]+$ ]]; then
    fail 20 "conflict: invalid label '$LABEL' (expected [a-z0-9-]+)"
  fi
elif [[ -n "${AGENTS_EXECUTOR:-}" ]]; then
  # Контекст сессии первичен: при вызове скрипта по абсолютному
  # пути репозитория навыков path-эвристика не работает, а окружение
  # диспетчеризованной сессии знает исполнителя точно.
  LABEL="$AGENTS_EXECUTOR"
  if [[ ! "$LABEL" =~ ^[a-z0-9-]+$ ]]; then
    fail 20 "conflict: invalid label '$LABEL' from \$AGENTS_EXECUTOR (expected [a-z0-9-]+)"
  fi
elif ! LABEL="$(detect_label "$SCRIPT_INVOKED_DIR")"; then
  fail 19 "conflict: cannot auto-detect label from '$SCRIPT_INVOKED_DIR' and \$AGENTS_EXECUTOR is not set, pass explicit [a-z0-9-]+ label as 3rd argument"
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  fail 1 "error: not a git repository"
fi

# AGENTS_LOCAL_ONLY=1 (режим local-only из protocol): только локальное
# git-состояние — origin не требуется, fetch не выполняется. Без флага
# origin и его fetch обязательны.
if ! origin_remote="$(git remote get-url origin 2>/dev/null)"; then
  [[ -n "${AGENTS_LOCAL_ONLY:-}" ]] || fail 15 "conflict: remote 'origin' is not configured"
  origin_remote=""
elif [[ -z "${AGENTS_LOCAL_ONLY:-}" ]] && ! git fetch origin --prune --quiet; then
  fail 16 "conflict: cannot fetch remote 'origin'"
fi

git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_repo_root="$(dirname "$git_common_dir")"
worktree_dir="$(dirname "$main_repo_root")/$(basename "$main_repo_root")-$KEY-$LABEL"

candidates=()
for candidate_type in "${branch_types[@]}"; do
  candidate="$candidate_type/$KEY"
  if has_local_branch "$candidate" || has_origin_branch "$candidate"; then
    candidates+=("$candidate")
  fi
done

if [[ ${#candidates[@]} -gt 1 ]]; then
  echo "conflict: multiple canonical branches exist for '$KEY', pick one:" >&2
  printf '  %s\n' "${candidates[@]}" >&2
  exit 11
fi

if [[ ${#candidates[@]} -eq 1 ]]; then
  branch="${candidates[0]}"
  worktree_branch="$branch-$LABEL"
  local_exists=false
  origin_exists=false
  has_local_branch "$branch" && local_exists=true
  has_origin_branch "$branch" && origin_exists=true

  if "$local_exists" && "$origin_exists" \
    && branches_diverged "refs/heads/$branch" "refs/remotes/origin/$branch"; then
    fail 12 "conflict: '$branch' diverged from origin, resolve manually"
  fi

  holder=""
  if "$local_exists"; then
    holder="$(worktree_holding_branch "$branch" || true)"
  fi

  if [[ -n "$holder" ]]; then
    fail 21 "conflict: canonical branch '$branch' is already checked out at '$holder', resolve manually"
  fi

  worktree_holder="$(worktree_holding_branch "$worktree_branch" || true)"
  if [[ -n "$worktree_holder" && "$worktree_holder" != "$worktree_dir" ]]; then
    fail 21 "conflict: agent branch '$worktree_branch' is already checked out at '$worktree_holder', resolve manually"
  fi

  if [[ -d "$worktree_dir" ]]; then
    if [[ "$worktree_holder" != "$worktree_dir" ]]; then
      fail 22 "conflict: path '$worktree_dir' exists but is not a registered git worktree for '$worktree_branch', resolve manually"
    fi

    # The worktree is registered for this branch; resume there. Only tracked
    # changes mark it dirty — untracked/ignored files are intentionally
    # ignored, same as the previous shared-checkout behavior.
    if worktree_is_dirty "$worktree_dir"; then
      echo "conflict: worktree has uncommitted changes on tracked files, commit or restore them first:" >&2
      git -C "$worktree_dir" status --short --untracked-files=no >&2
      exit 10
    fi

    # Локальная каноническая ветка могла быть удалена, пока worktree агента
    # существовал: восстанови её из origin до fast-forward проверок.
    if ! "$local_exists" && "$origin_exists"; then
      if ! git branch --no-track "$branch" "origin/$branch" >/dev/null 2>&1; then
        fail 18 "error: cannot create canonical branch '$branch' from origin"
      fi
      local_exists=true
    fi

    if "$origin_exists" \
      && git merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch" \
      && ! git branch -f "$branch" "origin/$branch" >/dev/null 2>&1; then
      fail 18 "error: cannot fast-forward canonical branch '$branch' from origin"
    fi

    # When the agent has not committed unique work yet, keep its clean branch
    # aligned with the newly fast-forwarded canonical branch. Once it has its
    # own commits, acceptance performs the explicit merge instead.
    if git merge-base --is-ancestor "$worktree_branch" "$branch" \
      && ! git -C "$worktree_dir" merge --ff-only --quiet "$branch" >/dev/null 2>&1; then
      fail 18 "error: cannot fast-forward agent branch '$worktree_branch' from '$branch'"
    fi

    echo "current: $branch via $worktree_branch at $worktree_dir"
    exit 0
  fi

  # A fetched origin ref is the source of truth when the local branch only
  # lags behind it, before the worktree is created.
  if "$local_exists" && "$origin_exists" \
    && git merge-base --is-ancestor "refs/heads/$branch" "refs/remotes/origin/$branch"; then
    if ! git branch -f "$branch" "origin/$branch" >/dev/null 2>&1; then
      fail 18 "error: cannot fast-forward local branch '$branch' from origin"
    fi
  fi

  if ! "$local_exists"; then
    if ! git branch --no-track "$branch" "origin/$branch" >/dev/null 2>&1; then
      fail 18 "error: cannot create canonical branch '$branch' from origin"
    fi
  fi

  if has_local_branch "$worktree_branch"; then
    if ! git worktree add --quiet "$worktree_dir" "$worktree_branch"; then
      fail 23 "error: cannot create worktree '$worktree_dir' for '$worktree_branch'"
    fi
  elif ! git worktree add --quiet --no-track -b "$worktree_branch" "$worktree_dir" "$branch"; then
    fail 23 "error: cannot create worktree '$worktree_dir' for '$worktree_branch'"
  fi

  echo "switched: $branch (existing) via $worktree_branch at $worktree_dir"
  exit 0
fi

if [[ -d "$worktree_dir" ]]; then
  fail 22 "conflict: path '$worktree_dir' exists but is not a registered git worktree for a branch of '$KEY', resolve manually"
fi

base_branch=${AGENTS_BASE_BRANCH:-develop}
local_base=false
origin_base=false
has_local_branch "$base_branch" && local_base=true
has_origin_branch "$base_branch" && origin_base=true

if ! "$local_base" && ! "$origin_base"; then
  if is_infrastructure_repository "$origin_remote"; then
    base_branch=${AGENTS_BASE_BRANCH_FALLBACK:-master}
    has_local_branch "$base_branch" && local_base=true
    has_origin_branch "$base_branch" && origin_base=true
  fi

  if ! "$local_base" && ! "$origin_base"; then
    fail 17 "conflict: base branch '${AGENTS_BASE_BRANCH:-develop}' is absent locally and on origin; the '${AGENTS_BASE_BRANCH_FALLBACK:-master}' fallback is available only for infrastructure repositories when it exists"
  fi
fi

if "$local_base" && "$origin_base" \
  && branches_diverged "refs/heads/$base_branch" "refs/remotes/origin/$base_branch"; then
  fail 13 "conflict: '$base_branch' diverged from origin, resolve manually before creating a new branch"
fi

if "$origin_base" \
  && { ! "$local_base" || git merge-base --is-ancestor "refs/heads/$base_branch" "refs/remotes/origin/$base_branch"; }; then
  base_ref="origin/$base_branch"
else
  base_ref="$base_branch"
fi

# При пропущенном fetch (AGENTS_LOCAL_ONLY) база берётся из устаревшего снимка —
# и локальный ref, и origin-ref из клона могут отставать от реального upstream,
# отставание молча уносится в новый worktree. Предупреждаем, но не
# блокируем — локальный режим намеренный.
if [[ -n "${AGENTS_LOCAL_ONLY:-}" || -z "$origin_remote" ]]; then
  echo "warning: base '$base_branch' resolved without origin sync (fetch skipped); it may lag behind upstream" >&2
fi

new_branch="$TYPE/$KEY"
worktree_branch="$new_branch-$LABEL"

if ! git branch --no-track "$new_branch" "$base_ref" >/dev/null 2>&1; then
  if has_local_branch "$new_branch"; then
    fail 14 "conflict: branch '$new_branch' appeared while creating it, resolve manually"
  fi
  fail 18 "error: cannot create canonical branch '$new_branch' from '$base_ref'"
fi

if ! git worktree add --quiet --no-track -b "$worktree_branch" "$worktree_dir" "$new_branch"; then
  git branch -D "$new_branch" >/dev/null 2>&1 || true
  fail 23 "error: cannot create worktree '$worktree_dir' for '$worktree_branch' from '$new_branch'"
fi

echo "created: $new_branch (from $base_branch) via $worktree_branch at $worktree_dir"
