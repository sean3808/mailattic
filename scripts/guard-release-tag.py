"""Block unverified bes-v* tags before Git pushes them.

The hook is deliberately read-only: it parses pre-push stdin, inspects Git
objects, and compares them with Git-local release evidence. Deployment remains
the responsibility of scripts/release.ps1.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_RELEASE_REF_PREFIX = "refs/tags/bes-v"
_EVIDENCE_NAME = "bes-release-state.json"
_EVIDENCE_KEYS = {
    "schema_version",
    "tag",
    "commit",
    "verified_at",
    "source_manifest_sha256",
}
_HEX_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


class GuardError(RuntimeError):
    """A fail-closed validation error suitable for operator output."""


def _git(repo_root: Path, *arguments: str, allowed: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if result.returncode not in allowed:
        raise GuardError(
            f"git {' '.join(arguments)} failed with exit code {result.returncode}"
        )
    return result.stdout.strip()


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise GuardError("cannot resolve the Git repository root")
    return Path(result.stdout.strip()).resolve()


def _is_zero_oid(object_id: str) -> bool:
    return bool(object_id) and set(object_id) == {"0"}


def _evidence_path(repo_root: Path) -> Path:
    raw_path = _git(repo_root, "rev-parse", "--git-path", _EVIDENCE_NAME)
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _read_evidence(repo_root: Path) -> dict[str, object]:
    path = _evidence_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
        evidence = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardError("release evidence is missing or malformed") from exc

    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_KEYS:
        raise GuardError("release evidence does not match schema version 1")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1:
        raise GuardError("release evidence does not match schema version 1")

    tag = evidence["tag"]
    commit = evidence["commit"]
    verified_at = evidence["verified_at"]
    manifest = evidence["source_manifest_sha256"]
    if not isinstance(tag, str) or not tag.startswith("bes-v"):
        raise GuardError("release evidence contains an invalid tag")
    if not isinstance(commit, str) or _HEX_OID.fullmatch(commit) is None:
        raise GuardError("release evidence contains an invalid commit")
    if not isinstance(manifest, str) or _SHA256.fullmatch(manifest) is None:
        raise GuardError("release evidence contains an invalid source manifest")
    if not isinstance(verified_at, str) or _UTC_RFC3339.fullmatch(verified_at) is None:
        raise GuardError("release evidence contains an invalid verification time")
    try:
        datetime.fromisoformat(verified_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise GuardError(
            "release evidence contains an invalid verification time"
        ) from exc
    return evidence


def _validate_release_update(
    repo_root: Path,
    *,
    local_ref: str,
    local_oid: str,
    remote_ref: str,
    remote_oid: str,
) -> None:
    if local_ref != remote_ref:
        raise GuardError("release tags must use the same local and remote reference")
    if not _is_zero_oid(remote_oid) and remote_oid != local_oid:
        raise GuardError("release tags are immutable once present on the remote")

    object_type = _git(repo_root, "cat-file", "-t", local_oid)
    if object_type != "tag":
        raise GuardError(
            "release tags must be annotated and created by the release workflow"
        )

    commit = _git(repo_root, "rev-parse", f"{local_oid}^{{commit}}")
    evidence = _read_evidence(repo_root)
    tag = remote_ref.removeprefix("refs/tags/")
    if evidence["tag"] != tag or evidence["commit"] != commit:
        raise GuardError("release evidence does not match the pushed tag and commit")


def guard_pre_push(repo_root: Path, lines: list[str]) -> int:
    for raw_line in lines:
        if not raw_line.strip():
            continue
        fields = raw_line.split()
        if len(fields) != 4:
            raise GuardError("malformed pre-push input")
        local_ref, local_oid, remote_ref, remote_oid = fields

        if not remote_ref.startswith(_RELEASE_REF_PREFIX):
            continue
        if _is_zero_oid(local_oid):
            continue
        if not local_ref.startswith(_RELEASE_REF_PREFIX):
            raise GuardError("release tag update has an unexpected local reference")

        tag = remote_ref.removeprefix("refs/tags/")
        try:
            _validate_release_update(
                repo_root,
                local_ref=local_ref,
                local_oid=local_oid,
                remote_ref=remote_ref,
                remote_oid=remote_oid,
            )
        except GuardError as exc:
            recovery = (
                f"pwsh -ExecutionPolicy Bypass -File scripts/release.ps1 -Tag {tag}"
            )
            print(
                f"[bes-release-guard] BLOCKED {tag}: {exc}\n"
                f"[bes-release-guard] Recovery: {recovery}",
                file=sys.stderr,
            )
            return 1
    return 0


def main() -> int:
    try:
        repo_root = _repo_root()
        return guard_pre_push(repo_root, list(sys.stdin))
    except GuardError as exc:
        print(f"[bes-release-guard] BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
