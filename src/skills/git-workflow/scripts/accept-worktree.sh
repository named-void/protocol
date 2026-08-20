#!/usr/bin/env bash
# Commit an accepted agent worktree and merge it into the canonical task branch.
#
# Usage: EXPECTED_CANONICAL_BASE=<full-sha> \
#        accept-worktree.sh <KEY> <branch-type> <commit-title> [label]
#
# Branches:
#   agent worktree: <type>/<KEY>-<label>
#   canonical task: <type>/<KEY>
#
# The order is deliberate and covered by tests:
#   0. release a foreign checkout of the canonical branch — never a worktree
#      of the same issue — when the switch is invisible to it: clean tree and
#      canonical standing on the base branch;
#   1. preflight both branches/worktrees and fetch origin;
#   2. stage and commit all accepted worktree changes;
#   3. switch that worktree from the agent branch to the canonical branch;
#   4. merge the agent branch into the canonical branch;
#   5. remove the clean worktree and delete the merged local agent branch.
#
# Exit codes:
#   0  accepted commit merged and worktree removed
#   1  not a git repository
#   10 worktree has no changes and agent branch has nothing to merge
#   12 canonical branch diverged from origin
#   15 origin remote is not configured (not required when AGENTS_LOCAL_ONLY=1)
#   16 cannot fetch origin
#   18 canonical branch cannot be updated or checked out
#   19 cannot auto-detect label and none was passed explicitly
#   20 invalid explicit label
#   21 canonical branch is checked out in another worktree that cannot be
#      released (it carries own commits on top of the base branch or has
#      uncommitted changes)
#   22 expected agent worktree is absent, registered for another branch, or
#      the agent branch is checked out elsewhere
#   24 commit failed
#   25 merge failed (the worktree is intentionally left for resolution)
#   26 cleanup failed after a successful merge
#   27 canonical branch moved after candidate acceptance; no integration started
#   64 usage error / invalid issue key or commit title
#   65 unknown branch type

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
  echo "usage: EXPECTED_CANONICAL_BASE=<full-sha> accept-worktree.sh <KEY> <branch-type> <commit-title> [label]" >&2
  exit 64
}

[[ $# -eq 3 || $# -eq 4 ]] || usage
KEY="$1"
TYPE="$2"
COMMIT_TITLE="$3"
LABEL="${4-}"

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
[[ "$COMMIT_TITLE" != *$'\n'* && "$COMMIT_TITLE" == "$KEY "* ]] \
  || fail 64 "conflict: commit title must be one line in format '$KEY <краткое описание в прошедшем времени>'"
EXPECTED_CANONICAL_BASE="${EXPECTED_CANONICAL_BASE:-}"
[[ "$EXPECTED_CANONICAL_BASE" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] \
  || fail 64 "conflict: EXPECTED_CANONICAL_BASE must be a full Git commit SHA"

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
elif ! LABEL="$(detect_label "$SCRIPT_INVOKED_DIR")"; then
  fail 19 "conflict: cannot auto-detect label from '$SCRIPT_INVOKED_DIR', pass explicit [a-z0-9-]+ label as 4th argument"
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail 1 "error: not a git repository"
# AGENTS_LOCAL_ONLY=1 (режим local-only из protocol): только локальное
# git-состояние — origin не требуется, fetch не выполняется. Без флага
# origin и его fetch обязательны.
if origin_remote="$(git remote get-url origin 2>/dev/null)"; then
  if [[ -z "${AGENTS_LOCAL_ONLY:-}" ]] && ! git fetch origin --prune --quiet; then
    fail 16 "conflict: cannot fetch remote 'origin'"
  fi
else
  origin_remote=""
  [[ -n "${AGENTS_LOCAL_ONLY:-}" ]] || fail 15 "conflict: remote 'origin' is not configured"
fi

git_common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_repo_root="$(dirname "$git_common_dir")"
worktree_dir="$(dirname "$main_repo_root")/$(basename "$main_repo_root")-$KEY-$LABEL"
canonical_branch="$TYPE/$KEY"
agent_branch="$canonical_branch-$LABEL"

has_local_branch "$canonical_branch" \
  || fail 22 "conflict: canonical branch '$canonical_branch' does not exist"
has_local_branch "$agent_branch" \
  || fail 22 "conflict: agent branch '$agent_branch' does not exist"

if ! expected_commit="$(git rev-parse --verify "${EXPECTED_CANONICAL_BASE}^{commit}" 2>/dev/null)" \
  || [[ "$expected_commit" != "$EXPECTED_CANONICAL_BASE" ]]; then
  fail 27 "stale: expected canonical base '$EXPECTED_CANONICAL_BASE' is unavailable"
fi

# Сравниваем с effective canonical после fetch, но до переключения веток,
# commit accepted-изменений и merge. Код 25 уже начал интеграцию и оставил
# canonical в task-worktree; его resume сохраняет прежний контракт.
preflight_canonical_holder="$(worktree_holding_branch "$canonical_branch" || true)"
preflight_agent_holder="$(worktree_holding_branch "$agent_branch" || true)"
integration_already_started=false
if [[ "$preflight_canonical_holder" == "$worktree_dir" && -z "$preflight_agent_holder" ]]; then
  integration_already_started=true
fi

if ! "$integration_already_started"; then
  effective_canonical_ref="refs/heads/$canonical_branch"
  if has_origin_branch "$canonical_branch"; then
    if branches_diverged "refs/heads/$canonical_branch" "refs/remotes/origin/$canonical_branch"; then
      fail 12 "conflict: '$canonical_branch' diverged from origin, resolve manually"
    fi
    if git merge-base --is-ancestor "refs/heads/$canonical_branch" "refs/remotes/origin/$canonical_branch"; then
      effective_canonical_ref="refs/remotes/origin/$canonical_branch"
    fi
  fi
  actual_canonical_base="$(git rev-parse "$effective_canonical_ref")"
  if [[ "$actual_canonical_base" != "$EXPECTED_CANONICAL_BASE" ]]; then
    fail 27 "stale: canonical branch '$canonical_branch' moved: expected '$EXPECTED_CANONICAL_BASE', got '$actual_canonical_base'"
  fi
fi

# Освобождает канон, занятый чужим checkout, — но только когда переключение
# для него незаметно: дерево чисто, а канон стоит ровно на локальной базовой
# ветке, то есть те же файлы остаются на диске. Свои коммиты поверх базы или
# незакоммиченные изменения оставляют отказ 21 вызывающему.
release_canonical_holder() {
  local holder="$1" base_branch="${AGENTS_BASE_BRANCH:-develop}"

  # Соседний worktree того же issue не трогаем: он встаёт на канон в приёмке,
  # и увод на базу лишает её resume-пути (её же код 25 ждёт worktree на каноне).
  if [[ "$holder" == "$(dirname "$main_repo_root")/$(basename "$main_repo_root")-$KEY-"* ]]; then
    return 1
  fi
  if ! has_local_branch "$base_branch"; then
    is_infrastructure_repository "$origin_remote" || return 1
    base_branch="${AGENTS_BASE_BRANCH_FALLBACK:-master}"
    has_local_branch "$base_branch" || return 1
  fi
  [[ "$(git rev-parse "refs/heads/$canonical_branch")" \
     == "$(git rev-parse "refs/heads/$base_branch")" ]] || return 1
  [[ -z "$(git -C "$holder" status --porcelain 2>/dev/null)" ]] || return 1
  git -C "$holder" switch --quiet "$base_branch" 2>/dev/null || return 1

  echo "released: '$holder' switched from '$canonical_branch' to '$base_branch'"
}

canonical_holder="$(worktree_holding_branch "$canonical_branch" || true)"
agent_holder="$(worktree_holding_branch "$agent_branch" || true)"
if [[ -d "$worktree_dir" && "$agent_holder" == "$worktree_dir" \
      && -n "$canonical_holder" && "$canonical_holder" != "$worktree_dir" ]]; then
  if release_canonical_holder "$canonical_holder"; then
    canonical_holder=""
  fi
fi

resume_merge=false
if [[ -d "$worktree_dir" && "$agent_holder" == "$worktree_dir" && -z "$canonical_holder" ]]; then
  : # Normal acceptance starts on the agent branch.
elif [[ -d "$worktree_dir" && "$canonical_holder" == "$worktree_dir" && -z "$agent_holder" ]]; then
  resume_merge=true # A previous acceptance stopped on a merge conflict.
elif [[ -n "$canonical_holder" && "$canonical_holder" != "$worktree_dir" ]]; then
  fail 21 "conflict: canonical branch '$canonical_branch' is already checked out at '$canonical_holder'"
else
  fail 22 "conflict: '$worktree_dir' is not registered for '$agent_branch' or a resumable '$canonical_branch' merge"
fi

if ! "$resume_merge" && has_origin_branch "$canonical_branch"; then
  if branches_diverged "refs/heads/$canonical_branch" "refs/remotes/origin/$canonical_branch"; then
    fail 12 "conflict: '$canonical_branch' diverged from origin, resolve manually"
  fi
  if git merge-base --is-ancestor "refs/heads/$canonical_branch" "refs/remotes/origin/$canonical_branch"; then
    git branch -f "$canonical_branch" "origin/$canonical_branch" >/dev/null 2>&1 \
      || fail 18 "error: cannot fast-forward canonical branch '$canonical_branch' from origin"
  fi
fi

if "$resume_merge"; then
  if [[ -n "$(git -C "$worktree_dir" diff --name-only --diff-filter=U)" ]]; then
    fail 25 "conflict: merge in '$worktree_dir' still has unresolved files"
  fi
  if git -C "$worktree_dir" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
    git -C "$worktree_dir" add -A
    git -C "$worktree_dir" commit --no-edit \
      || fail 25 "conflict: resolved merge in '$worktree_dir' could not be committed"
  fi
else
  if [[ -n "$(git -C "$worktree_dir" status --porcelain)" ]]; then
    git -C "$worktree_dir" add -A
    git -C "$worktree_dir" commit -m "$COMMIT_TITLE" \
      || fail 24 "error: cannot commit accepted changes in '$agent_branch'"
  elif git merge-base --is-ancestor "$agent_branch" "$canonical_branch"; then
    fail 10 "conflict: '$agent_branch' has no changes to merge into '$canonical_branch'"
  fi

  git -C "$worktree_dir" switch --quiet "$canonical_branch" \
    || fail 18 "error: cannot switch worktree to canonical branch '$canonical_branch'"
  if ! git -C "$worktree_dir" merge --no-edit "$agent_branch"; then
    fail 25 "conflict: cannot merge '$agent_branch' into '$canonical_branch'; resolve the merge in '$worktree_dir' and rerun this command"
  fi
fi

git merge-base --is-ancestor "$agent_branch" "$canonical_branch" \
  || fail 26 "error: merge succeeded, but '$agent_branch' is not contained in '$canonical_branch'"
# --force is safe here: the ancestry check above already confirms the
# merge landed on canonical_branch before the worktree (and any leftover
# untracked/ignored files in it) is discarded.
cd "$main_repo_root" \
  || fail 26 "error: merge succeeded, but main repository '$main_repo_root' is unavailable for cleanup"
git worktree remove --force "$worktree_dir" \
  || fail 26 "error: merge succeeded, but worktree '$worktree_dir' could not be removed"
git update-ref -d "refs/heads/$agent_branch" \
  || fail 26 "error: merge succeeded, but agent branch '$agent_branch' could not be deleted"

echo "accepted: $agent_branch -> $canonical_branch (commit: $COMMIT_TITLE)"
