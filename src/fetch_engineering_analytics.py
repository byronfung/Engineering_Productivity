#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_REPO_PATH = "/Users/byron/Documents/GitHub/posthog"
DEFAULT_OWNER = "PostHog"
DEFAULT_REPO = "posthog"
BOT_RE = re.compile(
    r"(\[bot\]$|-bot$|bot@|noreply\.github\.com.*bot|^github-actions$|"
    r"^copilot-pull-request-reviewer$|^chatgpt-codex-connector$|^cursor$|"
    r"-app(s)?$|-security$|^scheduled-actions-|^deployment-status-|"
    r"^tests-|^posthog-js-upgrader$|^posthog-local-dev$|^posthog$|"
    r"^force-merge-|^assign-reviewers-)",
    re.I,
)
REVERT_RE = re.compile(r"\b(revert|reverted|rollback|back\s*out|undo)\b", re.I)
PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def iso_date(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")


def contributor_key(name: str, email: str) -> str:
    return email.lower() if email else name.lower()


def blank_contributor(label: str) -> dict[str, Any]:
    return {
        "contributor": label,
        "display_name": label,
        "login": "",
        "email": "",
        "is_bot": bool(BOT_RE.search(label)),
        "commits_authored": 0,
        "prs_merged": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "lines_changed": 0,
        "reviews": 0,
        "approvals": 0,
        "comments": 0,
        "change_requests": 0,
        "revert_like_commits": 0,
    }


def run_git_log(repo_path: str, since: str, until: str) -> list[dict[str, Any]]:
    cmd = [
        "git",
        "-C",
        repo_path,
        "log",
        f"--since={since}T00:00:00Z",
        f"--until={until}T23:59:59Z",
        "--first-parent",
        "--format=%x1e%H%x1f%an%x1f%ae%x1f%s",
        "--numstat",
    ]
    proc = subprocess.run(cmd, check=True, text=True, capture_output=True)
    commits: list[dict[str, Any]] = []
    for record in proc.stdout.split("\x1e"):
        if not record.strip():
            continue
        header, *numstat_lines = record.splitlines()
        sha, name, email, subject = header.split("\x1f", 3)
        current = {
            "sha": sha,
            "author_name": name,
            "author_email": email,
            "subject": subject,
            "pr_number": int(PR_NUMBER_RE.search(subject).group(1)) if PR_NUMBER_RE.search(subject) else None,
            "lines_added": 0,
            "lines_deleted": 0,
            "revert_like": bool(REVERT_RE.search(subject)),
        }
        for raw_line in numstat_lines:
            parts = raw_line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                current["lines_added"] += int(parts[0])
                current["lines_deleted"] += int(parts[1])
        commits.append(current)
    return commits


def graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "engineering-impact-dashboard",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"GitHub GraphQL error {exc.code}: {detail}") from exc
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


PR_SEARCH_QUERY = """
query($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        mergedAt
        author { login }
        mergeCommit { oid }
        comments(first: 100) {
          nodes { author { login } createdAt }
        }
        reviews(first: 100) {
          nodes { author { login } state submittedAt }
        }
      }
    }
  }
  rateLimit { remaining resetAt }
}
"""


def date_chunks(start: dt.date, end: dt.date, chunk_days: int = 3) -> list[tuple[dt.date, dt.date]]:
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def fetch_prs(token: str, owner: str, repo: str, start: dt.date, end: dt.date) -> dict[int, dict[str, Any]]:
    prs: dict[int, dict[str, Any]] = {}
    for chunk_start, chunk_end in date_chunks(start, end):
        cursor = None
        while True:
            search = (
                f"repo:{owner}/{repo} is:pr is:merged "
                f"merged:{chunk_start.isoformat()}..{chunk_end.isoformat()}"
            )
            data = graphql(token, PR_SEARCH_QUERY, {"query": search, "cursor": cursor})
            rate = data["rateLimit"]
            if int(rate["remaining"]) < 50:
                reset_at = dt.datetime.fromisoformat(rate["resetAt"].replace("Z", "+00:00"))
                sleep_for = max(0, (reset_at - dt.datetime.now(dt.timezone.utc)).total_seconds()) + 5
                print(f"Rate limit low; sleeping {sleep_for:.0f}s until reset.", file=sys.stderr)
                time.sleep(sleep_for)
            search_data = data["search"]
            for node in search_data["nodes"]:
                if node:
                    prs[node["number"]] = node
            if not search_data["pageInfo"]["hasNextPage"]:
                break
            cursor = search_data["pageInfo"]["endCursor"]
    return prs


def apply_commit_metrics(people: dict[str, dict[str, Any]], commits: list[dict[str, Any]], pr_by_number: dict[int, dict[str, Any]]) -> None:
    for commit in commits:
        pr = pr_by_number.get(commit.get("pr_number"))
        login = pr.get("author", {}).get("login") if pr else ""
        if login:
            key = login.lower()
            label = login
        else:
            key = contributor_key(commit["author_name"], commit["author_email"])
            label = commit["author_name"]
        person = people.setdefault(key, blank_contributor(label))
        if login:
            person["login"] = login
            person["display_name"] = login
            person["contributor"] = login
        else:
            person["email"] = commit["author_email"]
        person["is_bot"] = person["is_bot"] or bool(BOT_RE.search(label)) or bool(BOT_RE.search(commit["author_email"]))
        person["commits_authored"] += 1
        person["lines_added"] += commit["lines_added"]
        person["lines_deleted"] += commit["lines_deleted"]
        person["lines_changed"] += commit["lines_added"] + commit["lines_deleted"]
        person["revert_like_commits"] += 1 if commit["revert_like"] else 0


def apply_github_metrics(people: dict[str, dict[str, Any]], prs: dict[int, dict[str, Any]]) -> None:
    for pr in prs.values():
        author = (pr.get("author") or {}).get("login")
        if author:
            person = people.setdefault(author.lower(), blank_contributor(author))
            person["login"] = author
            person["display_name"] = author
            person["contributor"] = author
            person["is_bot"] = person["is_bot"] or bool(BOT_RE.search(author))
            person["prs_merged"] += 1
        for comment in (pr.get("comments") or {}).get("nodes", []):
            login = (comment.get("author") or {}).get("login")
            if not login:
                continue
            person = people.setdefault(login.lower(), blank_contributor(login))
            person["login"] = login
            person["display_name"] = login
            person["contributor"] = login
            person["is_bot"] = person["is_bot"] or bool(BOT_RE.search(login))
            person["comments"] += 1
        for review in (pr.get("reviews") or {}).get("nodes", []):
            login = (review.get("author") or {}).get("login")
            if not login:
                continue
            person = people.setdefault(login.lower(), blank_contributor(login))
            person["login"] = login
            person["display_name"] = login
            person["contributor"] = login
            person["is_bot"] = person["is_bot"] or bool(BOT_RE.search(login))
            state = review.get("state")
            person["reviews"] += 1
            if state == "APPROVED":
                person["approvals"] += 1
            elif state == "CHANGES_REQUESTED":
                person["change_requests"] += 1
            elif state == "COMMENTED":
                person["comments"] += 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--out", default="")
    parser.add_argument("--skip-github", action="store_true")
    args = parser.parse_args()

    load_env(Path(".env"))
    end_dt = dt.datetime.now(dt.timezone.utc)
    start_dt = end_dt - dt.timedelta(days=args.days - 1)
    start = start_dt.date()
    end = end_dt.date()

    print(f"Reading local git commits for {start.isoformat()}..{end.isoformat()}", file=sys.stderr)
    commits = run_git_log(args.repo_path, start.isoformat(), end.isoformat())

    prs: dict[int, dict[str, Any]] = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token and not args.skip_github:
        print("Fetching merged PRs, reviews, approvals, comments, and change requests from GitHub.", file=sys.stderr)
        prs = fetch_prs(token, args.owner, args.repo, start, end)
    elif not args.skip_github:
        print("GITHUB_TOKEN not set; dashboard will include only local git metrics.", file=sys.stderr)

    people: dict[str, dict[str, Any]] = collections.OrderedDict()
    apply_commit_metrics(people, commits, prs)
    apply_github_metrics(people, prs)

    output = {
        "metadata": {
            "owner": args.owner,
            "repo": args.repo,
            "repo_path": args.repo_path,
            "days": args.days,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "commit_count": len(commits),
            "merged_pr_count": len(prs),
            "github_metadata_included": bool(prs),
        },
        "contributors": sorted(people.values(), key=lambda item: item["contributor"].lower()),
        "pull_requests": [
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": (pr.get("author") or {}).get("login"),
                "merged_at": pr["mergedAt"],
                "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
            }
            for pr in sorted(prs.values(), key=lambda item: item["number"])
        ],
    }
    out = Path(args.out or f"data/posthog_contributors_{args.days}d.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2))
    print(f"Wrote {out} with {len(output['contributors'])} contributors.", file=sys.stderr)


if __name__ == "__main__":
    main()
