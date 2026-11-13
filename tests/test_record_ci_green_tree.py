"""Exercise the writer against temporary repositories and fake GitHub responses."""

import json
import re
import subprocess
from datetime import UTC, datetime

import pytest

from scripts import record_ci_green_tree as writer


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked").write_text("initial", encoding="utf-8")
    git(tmp_path, "add", "tracked")
    git(tmp_path, "commit", "-qm", "initial")
    monkeypatch.chdir(tmp_path)
    tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    job = {
        "id": 42,
        "name": "hosted-full",
        "status": "completed",
        "conclusion": "success",
        "completed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "html_url": "https://example.invalid/job/42",
    }
    state = {
        "remote": True,
        "runs": [{"id": 1}],
        "jobs": [job],
        "annotations": [
            {"title": "tested-tree", "annotation_level": "notice", "message": tree}
        ],
    }
    original = writer.command

    def fake(*args):
        if args[0] == "git":
            return original(*args)
        if args[1] == "repo":
            return json.dumps({"nameWithOwner": "owner/repo"})
        path = args[-1]
        if "/commits/" in path:
            if not state["remote"]:
                raise subprocess.CalledProcessError(1, args)
            return "{}"
        if "/workflows/" in path:
            return json.dumps([{"workflow_runs": state["runs"]}])
        if "/jobs?" in path:
            return json.dumps([{"jobs": state["jobs"]}])
        if "/annotations?" in path:
            return json.dumps([state["annotations"]])
        raise AssertionError(args)

    monkeypatch.setattr(writer, "command", fake)
    return tmp_path, tree, state


def test_success_appends_strict_reader_record(setup):
    repo, tree, state = setup
    path = repo / ".git" / "closeout-green-trees.jsonl"
    path.write_bytes(b'{"old":true}\n')
    assert writer.main() == 0
    lines = path.read_bytes().splitlines()
    assert len(lines) == 2 and lines[0] == b'{"old":true}'
    pairs = json.loads(lines[1], object_pairs_hook=list)
    assert len(pairs) == len(dict(pairs)) == 8
    record = dict(pairs)
    assert set(record) == {
        "v",
        "tree",
        "mode",
        "exit_code",
        "lane",
        "finished_at",
        "evidence",
        "fixtures_manifest_sha256",
    }
    assert re.fullmatch(r"[0-9a-f]{40}", record["tree"]) and record["tree"] == tree
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["finished_at"])
    assert record == {
        "v": 1,
        "tree": tree,
        "mode": "committed",
        "exit_code": 0,
        "lane": "gha-windows",
        "finished_at": state["jobs"][0]["completed_at"],
        "evidence": "https://example.invalid/job/42",
        "fixtures_manifest_sha256": None,
    }
    assert path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "case",
    [
        "dirty",
        "untracked",
        "remote",
        "failed",
        "missing_runs",
        "missing_annotation",
        "mismatch",
        "newer_failure",
    ],
)
def test_refusal_never_writes(setup, case, capsys):
    repo, tree, state = setup
    path = repo / ".git" / "closeout-green-trees.jsonl"
    path.write_bytes(b"existing\n")
    if case == "dirty":
        (repo / "tracked").write_text("changed", encoding="utf-8")
    elif case == "untracked":
        (repo / "untracked").write_text("new", encoding="utf-8")
    elif case == "remote":
        state["remote"] = False
    elif case == "failed":
        state["jobs"][0]["conclusion"] = "failure"
    elif case == "missing_runs":
        state["runs"] = []
    elif case == "missing_annotation":
        state["annotations"] = []
    elif case == "mismatch":
        state["annotations"][0]["message"] = "b" * 40
    else:
        state["jobs"].append(
            {
                **state["jobs"][0],
                "id": 43,
                "conclusion": "failure",
                "completed_at": "2999-01-01T00:00:00Z",
            }
        )
    assert writer.main() != 0
    assert path.read_bytes() == b"existing\n"
    if case == "mismatch":
        assert "does not match" in capsys.readouterr().err
