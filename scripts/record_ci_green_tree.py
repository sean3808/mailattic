"""Record a GitHub-hosted full-suite result for the current committed tree."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_QUEUED_STATUSES = {"queued", "waiting", "pending", "requested"}


def command(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def api(path: str) -> list:
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", path))
    return pages


def _job_sort_key(job: dict[str, object]) -> tuple[datetime, int]:
    timestamp = (
        job.get("started_at") or job.get("created_at") or job.get("completed_at")
    )
    parsed = (
        datetime.fromisoformat(str(timestamp))
        if timestamp
        else datetime.min.replace(tzinfo=UTC)
    )
    raw_id = job.get("id", 0)
    return (parsed, int(raw_id) if isinstance(raw_id, int) else 0)


def inspect_hosted_full(
    repo: str, sha: str
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Return (status_payload, newest_job) for the newest hosted-full job on sha."""
    runs = api(
        f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100"
    )
    candidates: list[tuple[dict[str, object], dict[str, object]]] = []
    for page in runs:
        for run in page["workflow_runs"]:
            for job_page in api(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=all"
            ):
                for job in job_page["jobs"]:
                    if job.get("name") == "hosted-full":
                        candidates.append((job, run))

    if not candidates:
        return (
            {
                "state": "not_found",
                "conclusion": None,
                "url": None,
                "run_id": None,
                "run_attempt": None,
            },
            None,
        )

    newest_job, newest_run = max(candidates, key=lambda item: _job_sort_key(item[0]))
    status = newest_job.get("status")
    conclusion = newest_job.get("conclusion")
    if status in _QUEUED_STATUSES:
        state = "queued"
    elif status == "in_progress":
        state = "in_progress"
    elif status == "completed" and conclusion == "success":
        state = "success"
    elif status == "completed":
        state = "failed"
    else:
        state = "queued"

    payload = {
        "state": state,
        "conclusion": conclusion,
        "url": newest_run.get("html_url") or newest_job.get("html_url"),
        "run_id": newest_job.get("run_id") or newest_run.get("id"),
        "run_attempt": newest_job.get("run_attempt"),
    }
    return payload, newest_job


def _repo_name_with_owner() -> str:
    return json.loads(command("gh", "repo", "view", "--json", "nameWithOwner"))[
        "nameWithOwner"
    ]


def _parse_args(argv: list[str] | tuple[str, ...] = ()) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status-json",
        action="store_true",
        help="Print the newest hosted-full status as JSON and exit without writing.",
    )
    parser.add_argument(
        "--commit",
        help="Commit SHA to inspect (used with --status-json; defaults to HEAD).",
    )
    return parser.parse_args(list(argv))


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    try:
        args = _parse_args(argv)
        if args.status_json:
            sha = args.commit or command("git", "rev-parse", "HEAD")
            repo = _repo_name_with_owner()
            payload, _ = inspect_hosted_full(repo, sha)
            print(json.dumps(payload, separators=(",", ":")))
            return 0

        if command("git", "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("working tree has tracked changes or untracked files")
        sha = command("git", "rev-parse", "HEAD")
        tree = command("git", "rev-parse", "HEAD^{tree}")
        repo = _repo_name_with_owner()
        command("gh", "api", f"repos/{repo}/commits/{sha}")
        payload, job = inspect_hosted_full(repo, sha)
        if payload["state"] == "not_found" or job is None:
            raise ValueError("no hosted-full job found")
        if payload["state"] in {"queued", "in_progress"}:
            raise ValueError(f"newest hosted-full job is {payload['state']}")
        if payload["state"] != "success":
            raise ValueError("newest hosted-full job did not succeed")
        completed_raw = job.get("completed_at")
        if not isinstance(completed_raw, str) or not completed_raw:
            raise ValueError("newest hosted-full job is missing completed_at")
        completed_at = datetime.fromisoformat(completed_raw)
        # The closeout reader ignores records older than 30 days; refuse instead of
        # appending a line it would silently skip.
        if datetime.now(UTC) - completed_at > timedelta(days=30):
            raise ValueError("hosted-full job finished more than 30 days ago")
        annotations = [
            annotation
            for page in api(
                f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=100"
            )
            for annotation in page
        ]
        notices = [
            a
            for a in annotations
            if a.get("title") == "tested-tree" and a.get("annotation_level") == "notice"
        ]
        if not notices:
            raise ValueError("tested-tree notice missing")
        if not any(a.get("message") == tree for a in notices):
            raise ValueError("tested-tree notice does not match HEAD tree")
        finished = completed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "v": 1,
            "tree": tree,
            "mode": "committed",
            "exit_code": 0,
            "lane": "gha-windows",
            "finished_at": finished,
            "evidence": job["html_url"],
            "fixtures_manifest_sha256": None,
        }
        if (
            command("git", "status", "--porcelain", "--untracked-files=normal")
            or command("git", "rev-parse", "HEAD") != sha
            or command("git", "rev-parse", "HEAD^{tree}") != tree
        ):
            raise ValueError("working tree or HEAD changed during verification")
        common = Path(command("git", "rev-parse", "--git-common-dir"))
        with (common / "closeout-green-trees.jsonl").open("a+b") as stream:
            stream.seek(0, 2)
            needs_newline = False
            if stream.tell():
                stream.seek(-1, 2)
                needs_newline = stream.read(1) != b"\n"
            line = (
                ("\n" if needs_newline else "")
                + json.dumps(record, separators=(",", ":"))
                + "\n"
            )
            stream.write(line.encode("utf-8"))
        print("Recorded hosted-full green tree.")
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Cannot record green tree: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
