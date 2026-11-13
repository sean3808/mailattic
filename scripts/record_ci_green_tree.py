"""Record a GitHub-hosted full-suite result for the current committed tree."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def command(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def api(path: str) -> list:
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", path))
    return pages


def main() -> int:
    try:
        if command("git", "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("working tree has tracked changes or untracked files")
        sha = command("git", "rev-parse", "HEAD")
        tree = command("git", "rev-parse", "HEAD^{tree}")
        repo = json.loads(command("gh", "repo", "view", "--json", "nameWithOwner"))[
            "nameWithOwner"
        ]
        command("gh", "api", f"repos/{repo}/commits/{sha}")
        runs = api(
            f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100"
        )
        jobs = []
        for page in runs:
            for run in page["workflow_runs"]:
                for job_page in api(
                    f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=all"
                ):
                    for job in job_page["jobs"]:
                        if (
                            job["name"] == "hosted-full"
                            and job["status"] == "completed"
                            and job.get("completed_at")
                        ):
                            jobs.append(
                                (datetime.fromisoformat(job["completed_at"]), job)
                            )
        if not jobs:
            raise ValueError("no completed hosted-full job found")
        completed_at, job = max(jobs, key=lambda item: item[0])
        if job["conclusion"] != "success":
            raise ValueError("most recently completed hosted-full job did not succeed")
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
    sys.exit(main())
