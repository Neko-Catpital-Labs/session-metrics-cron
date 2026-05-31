#!/usr/bin/env bash
set -euo pipefail

log_cleanup() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] cleanup: $*"
}

safe_count() {
  local value="${1:-0}"
  [[ "$value" =~ ^[0-9]+$ ]] || value=0
  printf '%s\n' "$value"
}

collect_invoker_managed_worktree_branches() {
  local repo="$1"
  local worktrees_dir="$2"
  local current_worktree=""
  local current_branch=""

  emit_managed_branch() {
    [[ -n "$current_worktree" && -n "$current_branch" ]] || return 0
    [[ "$current_worktree" == "$worktrees_dir" || "$current_worktree" == "$worktrees_dir/"* ]] || return 0
    case "$current_branch" in
      main|master|develop|development|trunk) return 0 ;;
    esac
    printf '%s\n' "$current_branch"
  }

  while IFS= read -r line; do
    if [[ -z "$line" ]]; then
      emit_managed_branch
      current_worktree=""
      current_branch=""
      continue
    fi
    case "$line" in
      worktree\ *) current_worktree="${line#worktree }" ;;
      branch\ refs/heads/*) current_branch="${line#branch refs/heads/}" ;;
      branch\ *) current_branch="" ;;
    esac
  done < <(git -C "$repo" worktree list --porcelain 2>/dev/null || true)
  emit_managed_branch
}

cleanup_invoker_remote_branch() {
  local repo="$1"
  local remote="$2"
  local branch="$3"

  [[ -n "$branch" ]] || return 0
  git -C "$repo" remote get-url "$remote" >/dev/null 2>&1 || return 0

  REMOTE_DELETE_ATTEMPT_COUNT=$((REMOTE_DELETE_ATTEMPT_COUNT + 1))
  local output
  if output="$(git -C "$repo" push "$remote" ":refs/heads/$branch" 2>&1)"; then
    REMOTE_DELETE_SUCCESS_COUNT=$((REMOTE_DELETE_SUCCESS_COUNT + 1))
    log_cleanup "remote-delete-ok repo=$repo remote=$remote branch=$branch"
    return 0
  fi

  case "$output" in
    *"remote ref does not exist"*|*"not found"*|*"unable to delete"*"not found"*)
      REMOTE_DELETE_MISSING_COUNT=$((REMOTE_DELETE_MISSING_COUNT + 1))
      log_cleanup "remote-delete-missing repo=$repo remote=$remote branch=$branch"
      return 0
      ;;
  esac
  REMOTE_DELETE_WARNING_COUNT=$((REMOTE_DELETE_WARNING_COUNT + 1))
  log_cleanup "remote-delete-warn repo=$repo remote=$remote branch=$branch output=${output//$'\n'/ }"
  return 0
}

cleanup_invoker_managed_worktrees_and_refs() {
  [[ -n "${HOME:-}" && "$HOME" != "/" ]] || {
    log_cleanup "skip invalid HOME=${HOME:-}"
    return 0
  }

  local invoker_home="$HOME/.invoker"
  local repos_dir="$invoker_home/repos"
  local worktrees_dir="$invoker_home/worktrees"

  REPO_COUNT=0
  MANAGED_BRANCH_COUNT=0
  REMOTE_DELETE_ATTEMPT_COUNT=0
  REMOTE_DELETE_SUCCESS_COUNT=0
  REMOTE_DELETE_MISSING_COUNT=0
  REMOTE_DELETE_WARNING_COUNT=0
  WORKTREE_REMOVE_COUNT=0
  WORKTREE_REMOVE_WARNING_COUNT=0
  LOCAL_REF_DELETE_COUNT=0

  log_cleanup "start host=$(hostname 2>/dev/null || echo unknown) home=$HOME invoker_home=$invoker_home"
  df -h "$HOME" 2>/dev/null | sed 's/^/cleanup: df-before: /' || true
  df -ih "$HOME" 2>/dev/null | sed 's/^/cleanup: dfi-before: /' || true

  if [[ -d "$repos_dir" ]]; then
    local repo
    for repo in "$repos_dir"/*; do
      [[ -d "$repo/.git" ]] || continue
      REPO_COUNT=$((REPO_COUNT + 1))
      log_cleanup "repo-scan repo=$repo"

      local managed_branch
      while IFS= read -r managed_branch; do
        [[ -n "$managed_branch" ]] || continue
        MANAGED_BRANCH_COUNT=$((MANAGED_BRANCH_COUNT + 1))
        log_cleanup "managed-worktree-branch repo=$repo branch=$managed_branch"
        cleanup_invoker_remote_branch "$repo" origin "$managed_branch"
        cleanup_invoker_remote_branch "$repo" upstream "$managed_branch"
      done < <(collect_invoker_managed_worktree_branches "$repo" "$worktrees_dir")

      local worktree
      while IFS= read -r worktree; do
        [[ -n "$worktree" ]] || continue
        if [[ "$worktree" == "$worktrees_dir" || "$worktree" == "$worktrees_dir/"* ]]; then
          if git -C "$repo" worktree remove --force "$worktree" >/dev/null 2>&1; then
            WORKTREE_REMOVE_COUNT=$((WORKTREE_REMOVE_COUNT + 1))
            log_cleanup "worktree-remove-ok repo=$repo path=$worktree"
          else
            WORKTREE_REMOVE_WARNING_COUNT=$((WORKTREE_REMOVE_WARNING_COUNT + 1))
            log_cleanup "worktree-remove-warn repo=$repo path=$worktree"
          fi
        fi
      done < <(git -C "$repo" worktree list --porcelain 2>/dev/null | awk '/^worktree / { sub(/^worktree /, ""); print }' || true)

      git -C "$repo" worktree prune >/dev/null 2>&1 || true
      while IFS= read -r ref; do
        [[ -n "$ref" ]] || continue
        if git -C "$repo" update-ref -d "$ref" >/dev/null 2>&1; then
          LOCAL_REF_DELETE_COUNT=$((LOCAL_REF_DELETE_COUNT + 1))
          log_cleanup "local-ref-delete-ok repo=$repo ref=$ref"
        else
          log_cleanup "local-ref-delete-warn repo=$repo ref=$ref"
        fi
      done < <(git -C "$repo" for-each-ref --format='%(refname)' \
        refs/heads/experiment refs/heads/invoker refs/heads/reconciliation 2>/dev/null || true)
    done
  else
    log_cleanup "repos-dir-missing path=$repos_dir"
  fi

  rm -rf "$worktrees_dir" 2>/dev/null || true

  log_cleanup "summary repos=$(safe_count "$REPO_COUNT") managed_branches=$(safe_count "$MANAGED_BRANCH_COUNT") remote_delete_attempts=$(safe_count "$REMOTE_DELETE_ATTEMPT_COUNT") remote_delete_ok=$(safe_count "$REMOTE_DELETE_SUCCESS_COUNT") remote_delete_missing=$(safe_count "$REMOTE_DELETE_MISSING_COUNT") remote_delete_warnings=$(safe_count "$REMOTE_DELETE_WARNING_COUNT") worktrees_removed=$(safe_count "$WORKTREE_REMOVE_COUNT") worktree_remove_warnings=$(safe_count "$WORKTREE_REMOVE_WARNING_COUNT") local_refs_deleted=$(safe_count "$LOCAL_REF_DELETE_COUNT")"
  df -h "$HOME" 2>/dev/null | sed 's/^/cleanup: df-after: /' || true
  df -ih "$HOME" 2>/dev/null | sed 's/^/cleanup: dfi-after: /' || true
}

cleanup_invoker_managed_worktrees_and_refs "$@"
