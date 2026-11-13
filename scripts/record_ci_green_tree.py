"""Record a GitHub-hosted full-suite result for the current committed tree."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
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
                    jobs.extend(
                        job
                        for job in job_page["jobs"]
                        if job["name"] == "hosted-full"
                        and job["status"] == "completed"
                        and job.get("completed_at")
                    )
        if not jobs:
            raise ValueError("no completed hosted-full job found")
        job = max(
            jobs,
            key=lambda item: datetime.fromisoformat(
                item["completed_at"].replace("Z", "+00:00")
            ),
        )
        if job["conclusion"] != "success":
            raise ValueError("most recently completed hosted-full job did not succeed")
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
        finished = (
            datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
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
        with (common / "closeout-green-trees.jsonl").open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        print("Recorded hosted-full green tree.")
        return 0
    except (
        OSError,
        subprocess.CalledProcessError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"Cannot record green tree: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
