#!/usr/bin/env python3
"""GitHub CLI (gh) wrappers using parameterized subprocess calls."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from espaut.security import validate_subprocess_args


class GitHubQueryError(RuntimeError):
    """A failed remote query is not evidence of an empty result."""


def _parse_json_response(response, operation: str, *, allow_nonzero: bool = False):
    if response.returncode != 0 and not allow_nonzero:
        raise GitHubQueryError(f"{operation} failed (exit={response.returncode}): {response.stderr[:300]}")
    try:
        value = json.loads(response.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GitHubQueryError(f"{operation}: invalid or missing JSON (exit={response.returncode})") from exc
    return value


def run_gh(
    *args: str,
    timeout: int = 60,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command with validated arguments."""
    cmd = ["gh", *args]
    validate_subprocess_args(cmd)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def gh_available() -> bool:
    """Return True if the gh CLI is installed and authenticated."""
    r = run_gh("auth", "status", timeout=10)
    return r.returncode == 0


def pr_list_json(
    fields: list[str] | None = None,
    state: str = "open",
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """List PRs as parsed JSON."""
    field_list = fields or ["number", "title", "headRefName", "state", "url"]
    r = run_gh(
        "pr", "list",
        "--state", state,
        "--json", ",".join(field_list),
        cwd=cwd,
    )
    result = _parse_json_response(r, "gh pr list")
    if not isinstance(result, list):
        raise GitHubQueryError("gh pr list: expected JSON array")
    return result


def pr_view_json(
    pr_ref: str | int,
    fields: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """Query a PR's fields as JSON."""
    field_list = fields or [
        "number", "title", "headRefName", "baseRefName",
        "state", "url", "headRefOid", "mergeCommit", "mergedAt",
    ]
    r = run_gh(
        "pr", "view", str(pr_ref),
        "--json", ",".join(field_list),
        cwd=cwd,
    )
    result = _parse_json_response(r, "gh pr view")
    if not isinstance(result, dict):
        raise GitHubQueryError("gh pr view: expected JSON object")
    return result


def pr_create(
    title: str,
    body: str,
    base: str | None = None,
    head: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """Create a PR and return its data (via pr view after creation).

    ``gh pr create`` outputs the new PR's URL to stdout; ``--json`` is not
    supported.  After a successful create we call ``pr_view_json`` to
    retrieve the structured representation.
    """
    args = ["pr", "create", "--title", title, "--body", body]
    if base:
        args.extend(["--base", base])
    if head:
        args.extend(["--head", head])
    r = run_gh(*args, cwd=cwd)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    view = pr_view_json(url, cwd=cwd) if url else None
    if view and view.get("number") and view.get("url"):
        return view
    raise GitHubQueryError("PR created but its identity could not be verified")


def pr_checks_json(
    pr_ref: str | int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Query check/status results for a PR."""
    r = run_gh(
        "pr", "checks", str(pr_ref),
        "--json", "name,state,bucket,description,startedAt,completedAt,link,workflow",
        cwd=cwd,
    )
    # gh pr checks can exit non-zero for checks that failed or are pending;
    # valid JSON is still authoritative data and must not be discarded.
    if r.returncode != 0 and not r.stdout.strip():
        # gh exits 1 when a PR has no checks. Distinguish a verified empty
        # rollup from transport/auth failures and avoid a false "no CI".
        if "no checks reported" in (r.stderr or "").lower():
            first = pr_view_json(pr_ref, ["headRefOid", "statusCheckRollup"], cwd=cwd)
            second = pr_view_json(pr_ref, ["headRefOid", "statusCheckRollup"], cwd=cwd)
            if (first and second and first.get("headRefOid")
                    and first.get("headRefOid") == second.get("headRefOid")
                    and first.get("statusCheckRollup") == []
                    and second.get("statusCheckRollup") == []):
                return []
        raise GitHubQueryError(f"gh pr checks failed (exit={r.returncode}): {r.stderr[:300]}")
    result = _parse_json_response(r, "gh pr checks", allow_nonzero=True)
    if not isinstance(result, list):
        raise GitHubQueryError("gh pr checks: expected JSON array")
    return result


def _paginated_list(response, label: str) -> list[dict[str, Any]]:
    data = _parse_json_response(response, label)
    if not isinstance(data, list):
        raise GitHubQueryError(f"{label}: expected JSON list")
    if data and isinstance(data[0], list):
        return [row for page in data for row in page]
    return data


def pr_review_threads_json(pr_number: int, cwd: str | None = None) -> list[dict[str, Any]]:
    """Retrieve GitHub review-thread resolution, refusing incomplete pagination."""
    repo_response = run_gh("repo", "view", "--json", "nameWithOwner", cwd=cwd)
    repo_json = _parse_json_response(repo_response, "gh repo view")
    name_with_owner = repo_json.get("nameWithOwner", "")
    if "/" not in name_with_owner:
        raise GitHubQueryError("repository identity unavailable")
    owner, repo = name_with_owner.split("/", 1)
    query = (
        "query($owner:String!,$repo:String!,$pr:Int!){"
        "repository(owner:$owner,name:$repo){pullRequest(number:$pr){"
        "reviewThreads(first:100){nodes{id isResolved isOutdated "
        "comments(first:100){nodes{databaseId} pageInfo{hasNextPage}}}"
        "pageInfo{hasNextPage}}}}}"
    )
    response = run_gh(
        "api", "graphql",
        "-f", f"query={query}", "-f", f"owner={owner}",
        "-f", f"repo={repo}", "-F", f"pr={pr_number}", cwd=cwd,
    )
    data = _parse_json_response(response, "gh api graphql")
    if data.get("errors"):
        raise GitHubQueryError("GitHub GraphQL returned errors")
    try:
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes = threads["nodes"]
        if threads["pageInfo"]["hasNextPage"]:
            raise GitHubQueryError("review threads exceed 100: pagination required")
        if any(n["comments"]["pageInfo"]["hasNextPage"] for n in nodes):
            raise GitHubQueryError("review thread comments exceed 100: pagination required")
        return nodes
    except (KeyError, TypeError, AttributeError) as exc:
        raise GitHubQueryError("incomplete review thread response") from exc


def pr_reviews_json(
    pr_number: int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Query reviews for a PR via the REST API."""
    r = run_gh(
        "api", "--paginate", "--slurp",
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
        cwd=cwd,
    )
    return _paginated_list(r, "gh api")


def pr_comments_json(
    pr_number: int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Query issue-level comments on a PR."""
    r = run_gh(
        "api", "--paginate", "--slurp",
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        cwd=cwd,
    )
    return _paginated_list(r, "gh api")


def pr_review_comments_json(
    pr_number: int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Query code-level review comments on a PR."""
    r = run_gh(
        "api", "--paginate", "--slurp",
        f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments",
        cwd=cwd,
    )
    return _paginated_list(r, "gh api")


def pr_merge(
    pr_ref: str | int,
    method: str = "merge",
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Merge a PR. method: merge, squash, or rebase.

    ``gh pr merge`` does not support ``--json``; success is indicated by
    a zero return code.
    """
    if method not in ("merge", "squash", "rebase"):
        raise ValueError("invalid merge method")
    return run_gh(
        "pr", "merge", str(pr_ref),
        f"--{method}",
        cwd=cwd,
    )
