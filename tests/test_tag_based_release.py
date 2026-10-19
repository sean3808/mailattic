"""Behavior tests for the explicit tag-based local release workflow."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._pwsh_support import (
    GIT_AUTO_MAINTENANCE_SETTING,
    disable_auto_maintenance_in_bare_repo,
    git_config_origin_and_value,
    git_env_without_auto_maintenance,
    ps_quote,
    run_pwsh_command,
    run_pwsh_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.ps1"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "guard-release-tag.py"
HOOK_INSTALLER_SCRIPT = REPO_ROOT / "scripts" / "install-release-pre-push.ps1"
ZERO_OID = "0" * 40


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=git_env_without_auto_maintenance(env),
    )


def _run_release_function(command: str) -> subprocess.CompletedProcess[str]:
    script = f". {ps_quote(RELEASE_SCRIPT)}; {command}"
    return run_pwsh_command(script, check=False, env=git_env_without_auto_maintenance())


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _make_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(tmp_path, "init", "--bare", str(remote))
    # The push below starts receive-pack inside the remote, where Git has
    # already dropped the environment-supplied override.
    disable_auto_maintenance_in_bare_repo(remote)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


@pytest.mark.parametrize(
    ("tag", "expected_valid"),
    [
        ("bes-v2026.07.18.1", True),
        ("v2026.07.18.1", False),
        ("bes-v2026.02.30.1", False),
        ("bes-v2026.07.18.0", False),
    ],
)
def test_release_tag_validation_examples(tag: str, expected_valid: bool):
    result = _run_release_function(
        f"if (Test-BesReleaseTag -Tag {ps_quote(tag)}) {{ exit 0 }} else {{ exit 9 }}"
    )
    assert (result.returncode == 0) is expected_valid, result.stderr


@pytest.mark.release
def test_release_preconditions_accept_clean_main_and_return_full_commit(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    expected = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'origin'"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_release_preconditions_reject_any_dirty_worktree(
    tmp_path: Path, dirty_kind: str
):
    repo, _ = _make_repo_with_remote(tmp_path)
    target = repo / ("tracked.txt" if dirty_kind == "tracked" else "untracked.txt")
    target.write_text("dirty\n", encoding="utf-8")

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'origin'"
    )

    assert result.returncode != 0
    assert "worktree" in result.stderr.lower()


def test_release_preconditions_reject_non_main_branch(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    _git(repo, "switch", "-c", "feature/not-a-release")

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'origin'"
    )

    assert result.returncode != 0
    assert "main" in result.stderr


def test_release_preconditions_reject_existing_local_tag(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    _git(repo, "tag", "-a", "bes-v2026.07.18.1", "-m", "existing")

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'origin'"
    )

    assert result.returncode != 0
    assert "already exists locally" in result.stderr


@pytest.mark.release
def test_release_preconditions_reject_existing_remote_tag(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    _git(repo, "tag", "-a", "bes-v2026.07.18.1", "-m", "existing")
    _git(repo, "push", "origin", "refs/tags/bes-v2026.07.18.1")
    _git(repo, "tag", "-d", "bes-v2026.07.18.1")

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'origin'"
    )

    assert result.returncode != 0
    assert "already exists on remote" in result.stderr


def test_release_preconditions_reject_missing_remote(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)

    result = _run_release_function(
        "Assert-ReleasePreconditions "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' -Remote 'missing'"
    )

    assert result.returncode != 0
    assert "remote" in result.stderr.lower()


def _make_matching_package_trees(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    source.mkdir()
    installed.mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (installed / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    nested_source = source / "nested"
    nested_installed = installed / "nested"
    nested_source.mkdir()
    nested_installed.mkdir()
    (nested_source / "module.py").write_text("NAME = 'bes'\n", encoding="utf-8")
    (nested_installed / "module.py").write_text("NAME = 'bes'\n", encoding="utf-8")
    return source, installed


def test_matching_package_manifest_returns_lowercase_sha256(tmp_path: Path):
    source, installed = _make_matching_package_trees(tmp_path)

    result = _run_release_function(
        "Get-MatchingPackageManifest "
        f"-SourceRoot {ps_quote(source)} -InstalledRoot {ps_quote(installed)}"
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip()) == 64
    assert result.stdout.strip() == result.stdout.strip().lower()
    int(result.stdout.strip(), 16)


@pytest.mark.parametrize("failure_kind", ["missing", "different"])
def test_package_manifest_rejects_stale_or_incomplete_installation(
    tmp_path: Path, failure_kind: str
):
    source, installed = _make_matching_package_trees(tmp_path)
    target = installed / "nested" / "module.py"
    if failure_kind == "missing":
        target.unlink()
    else:
        target.write_text("NAME = 'stale'\n", encoding="utf-8")

    result = _run_release_function(
        "Get-MatchingPackageManifest "
        f"-SourceRoot {ps_quote(source)} -InstalledRoot {ps_quote(installed)}"
    )

    assert result.returncode != 0
    assert failure_kind in result.stderr.lower() or "mismatch" in result.stderr.lower()


def test_package_manifest_excludes_cache_files(tmp_path: Path):
    source, installed = _make_matching_package_trees(tmp_path)
    source_cache = source / "__pycache__"
    installed_cache = installed / "__pycache__"
    source_cache.mkdir()
    installed_cache.mkdir()
    (source_cache / "module.cpython-313.pyc").write_bytes(b"source-cache")
    (installed_cache / "module.cpython-313.pyc").write_bytes(b"installed-cache")

    result = _run_release_function(
        "Get-MatchingPackageManifest "
        f"-SourceRoot {ps_quote(source)} -InstalledRoot {ps_quote(installed)}"
    )

    assert result.returncode == 0, result.stderr


def test_installed_release_requires_bes_help_and_matching_package(tmp_path: Path):
    source, installed = _make_matching_package_trees(tmp_path)
    fake_bes = tmp_path / "bes.cmd"
    fake_bes.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    result = _run_release_function(
        "Assert-InstalledRelease "
        f"-SourceRoot {ps_quote(source)} "
        f"-InstalledPackageRoot {ps_quote(installed)} "
        f"-BesExecutable {ps_quote(fake_bes)}"
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip()) == 64


def test_installed_release_rejects_failed_bes_help(tmp_path: Path):
    source, installed = _make_matching_package_trees(tmp_path)
    fake_bes = tmp_path / "bes.cmd"
    fake_bes.write_text("@echo off\r\nexit /b 7\r\n", encoding="utf-8")

    result = _run_release_function(
        "Assert-InstalledRelease "
        f"-SourceRoot {ps_quote(source)} "
        f"-InstalledPackageRoot {ps_quote(installed)} "
        f"-BesExecutable {ps_quote(fake_bes)}"
    )

    assert result.returncode != 0
    assert "bes --help" in result.stderr


def test_uv_tool_python_is_used_to_resolve_installed_package(tmp_path: Path):
    installed = tmp_path / "tool" / "Lib" / "site-packages" / "mailattic"
    installed.mkdir(parents=True)
    log = tmp_path / "python-args.txt"
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text(
        f'@echo off\r\necho %* > "{log}"\r\necho {installed}\r\n',
        encoding="utf-8",
    )

    result = _run_release_function(
        f"Get-InstalledPackagePath -ToolPythonOverride {ps_quote(fake_python)}"
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == installed
    args = log.read_text(encoding="utf-8")
    assert "-I" in args
    assert "import mailattic" in args


def test_release_evidence_has_exact_schema_and_is_git_local(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    manifest = "a" * 64

    result = _run_release_function(
        "Write-ReleaseEvidence "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' "
        f"-Commit {ps_quote(commit)} -ManifestHash {ps_quote(manifest)}"
    )

    assert result.returncode == 0, result.stderr
    evidence_path = Path(result.stdout.strip())
    assert evidence_path == repo / ".git" / "bes-release-state.json"
    raw = evidence_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    evidence = json.loads(raw)
    assert set(evidence) == {
        "schema_version",
        "tag",
        "commit",
        "verified_at",
        "source_manifest_sha256",
    }
    assert evidence["schema_version"] == 1
    assert evidence["tag"] == "bes-v2026.07.18.1"
    assert evidence["commit"] == commit
    assert evidence["source_manifest_sha256"] == manifest
    assert evidence["verified_at"].endswith("Z")


def test_evidence_write_failure_preserves_prior_record(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    evidence_path = repo / ".git" / "bes-release-state.json"
    evidence_path.write_text('{"previous":true}\n', encoding="utf-8")
    temp_path = repo / ".git" / "bes-release-state.json.tmp"
    temp_path.mkdir()
    before = evidence_path.read_bytes()

    result = _run_release_function(
        "Write-ReleaseEvidence "
        f"-RepoRoot {ps_quote(repo)} "
        "-Tag 'bes-v2026.07.18.1' "
        f"-Commit {ps_quote('a' * 40)} -ManifestHash {ps_quote('b' * 64)}"
    )

    assert result.returncode != 0
    assert evidence_path.read_bytes() == before


@pytest.mark.parametrize("change_kind", ["dirty", "new-commit", "branch"])
def test_release_snapshot_recheck_detects_change(tmp_path: Path, change_kind: str):
    repo, _ = _make_repo_with_remote(tmp_path)
    expected = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if change_kind == "dirty":
        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif change_kind == "new-commit":
        (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-m", "second")
    else:
        _git(repo, "switch", "-c", "feature/same-commit")

    result = _run_release_function(
        "Assert-ReleaseSnapshot "
        f"-RepoRoot {ps_quote(repo)} -ExpectedCommit {ps_quote(expected)}"
    )

    assert result.returncode != 0
    assert "changed" in result.stderr.lower() or "worktree" in result.stderr.lower()


def _make_release_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo, remote = _make_repo_with_remote(tmp_path)
    scripts = repo / "scripts"
    package = repo / "src" / "mailattic"
    scripts.mkdir()
    package.mkdir(parents=True)
    shutil.copy2(RELEASE_SCRIPT, scripts / "release.ps1")
    shutil.copy2(REPO_ROOT / "scripts/deploy-skill-to-ssot.py", scripts)
    skill = repo / "skills/mailattic"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("released skill\n", encoding="utf-8")
    (scripts / "reinstall-global.ps1").write_text(
        "throw 'test must override Invoke-GlobalReinstall'\n", encoding="utf-8"
    )
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "scripts", "src", "skills")
    _git(repo, "commit", "-m", "add release fixture")
    _git(repo, "push", "origin", "main")
    return repo, remote


def _release_test_overrides(
    installed_path: Path, *, override_tests: bool = True
) -> str:
    tests_override = (
        """
function Invoke-ReleaseTests {
    param([string]$RepoRoot)
    Write-Host 'OVERRIDE_TESTS'
}
"""
        if override_tests
        else ""
    )
    return f"""
{tests_override}
function Invoke-GlobalReinstall {{
    param([string]$RepoRoot)
    Write-Host 'OVERRIDE_REINSTALL'
}}
function Get-InstalledPackagePath {{
    return {ps_quote(installed_path)}
}}
function Assert-InstalledRelease {{
    param(
        [string]$SourceRoot,
        [string]$InstalledPackageRoot,
        [string]$BesExecutable = 'bes'
    )
    Write-Host 'OVERRIDE_VERIFY'
    return '{"a" * 64}'
}}
"""


def _run_release_driver(
    tmp_path: Path,
    repo: Path,
    *,
    tag: str = "bes-v2026.07.18.1",
    extra_script: str = "",
    override_tests: bool = True,
) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / "release-driver.ps1"
    installed = repo / "src" / "mailattic"
    driver.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$env:MAILATTIC_SKILL_SSOT = {ps_quote(tmp_path / "ssot")}
. {ps_quote(repo / "scripts" / "release.ps1")}
{_release_test_overrides(installed, override_tests=override_tests)}
{extra_script}
try {{
    Invoke-BesRelease -RepoRoot {ps_quote(repo)} -Tag {ps_quote(tag)} -Remote 'origin'
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
""",
        encoding="utf-8",
    )
    return run_pwsh_file(driver, check=False, env=git_env_without_auto_maintenance())


@pytest.mark.release
def test_release_orchestration_orders_stages_and_pushes_annotated_tag(
    tmp_path: Path,
):
    repo, remote = _make_release_repo(tmp_path)
    tag = "bes-v2026.07.18.1"

    result = _run_release_driver(tmp_path, repo, tag=tag)

    assert result.returncode == 0, result.stderr
    stage_markers = [
        "Validate release inputs",
        "Run full test suite",
        "Reinstall global bes",
        "Verify installed snapshot",
        "Sync and verify skill SSOT",
        "Write local release evidence",
        "Create annotated release tag",
        "Atomically push main and release tag",
    ]
    positions = [result.stdout.index(marker) for marker in stage_markers]
    assert positions == sorted(positions)
    assert (tmp_path / "ssot/SKILL.md").read_text(
        encoding="utf-8"
    ) == "released skill\n"
    assert _git(repo, "cat-file", "-t", f"refs/tags/{tag}").stdout.strip() == "tag"
    local_commit = _git(repo, "rev-parse", f"{tag}^{{commit}}").stdout.strip()
    remote_commit = _git(
        repo, "ls-remote", remote.as_posix(), f"refs/tags/{tag}^{{}}"
    ).stdout.split()[0]
    assert remote_commit == local_commit
    assert (
        json.loads(
            (repo / ".git" / "bes-release-state.json").read_text(encoding="utf-8")
        )["commit"]
        == local_commit
    )


@pytest.mark.parametrize("existing", ["missing", "stale", "matching"])
def test_release_syncs_tracked_skill_only(tmp_path: Path, existing: str):
    repo, _ = _make_release_repo(tmp_path)
    ssot = tmp_path / "ssot"
    ssot.mkdir()
    (ssot / "unrelated.txt").write_text("keep", encoding="utf-8")
    if existing != "missing":
        (ssot / "SKILL.md").write_text(
            "released skill\n" if existing == "matching" else "old", encoding="utf-8"
        )
    (repo / ".git/info/exclude").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "skills/mailattic/ignored.txt").write_text("secret")

    result = _run_release_driver(tmp_path, repo)

    assert result.returncode == 0, result.stderr
    assert (ssot / "SKILL.md").read_text(encoding="utf-8") == "released skill\n"
    assert (ssot / "unrelated.txt").read_text() == "keep"
    assert not (ssot / "ignored.txt").exists()


@pytest.mark.parametrize(
    ("fault", "detail"),
    [
        (
            "def fail_copy(*args): raise OSError('copy failed')\n"
            "shutil.copy2 = fail_copy",
            "copy failed",
        ),
        ("def is_on_main(*args): return False", "sync skipped"),
        ("def deploy(*args): pass", "tracked contents differ"),
    ],
)
def test_skill_failure_stops_release_before_success_artifacts(
    tmp_path: Path, fault: str, detail: str
):
    repo, remote = _make_release_repo(tmp_path)
    deploy = repo / "scripts/deploy-skill-to-ssot.py"
    deploy.write_text(
        deploy.read_text(encoding="utf-8").replace(
            'if __name__ == "__main__":', f'{fault}\n\nif __name__ == "__main__":'
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "scripts/deploy-skill-to-ssot.py")
    _git(repo, "commit", "-m", "inject boundary failure")
    evidence = repo / ".git/bes-release-state.json"
    evidence.write_bytes(b'{"previous":true}\n')

    result = _run_release_driver(tmp_path, repo)

    assert result.returncode != 0
    assert detail in result.stderr
    assert "already installed" in result.stderr
    assert "retry scripts/release.ps1" in result.stderr
    assert evidence.read_bytes() == b'{"previous":true}\n'
    assert not _git(repo, "tag", "--list").stdout.strip()
    assert not _git(repo, "ls-remote", str(remote), "refs/tags/*").stdout.strip()
    assert "Write local release evidence" not in result.stdout


@pytest.mark.release
def test_test_failure_short_circuits_without_tag_or_new_evidence(tmp_path: Path):
    repo, _ = _make_release_repo(tmp_path)
    old_evidence = repo / ".git" / "bes-release-state.json"
    old_evidence.write_text('{"previous":true}\n', encoding="utf-8")
    before = old_evidence.read_bytes()
    failure_override = """
function Invoke-ReleaseTests {
    param([string]$RepoRoot)
    throw 'simulated pytest failure'
}
"""

    result = _run_release_driver(tmp_path, repo, extra_script=failure_override)

    assert result.returncode != 0
    assert "simulated pytest failure" in result.stderr
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/tags/bes-v2026.07.18.1",
            check=False,
        ).returncode
        != 0
    )
    assert old_evidence.read_bytes() == before
    assert "Reinstall global bes" not in result.stdout


@pytest.mark.release
@pytest.mark.parametrize("prior_evidence", [False, True])
def test_tag_creation_failure_restores_prior_evidence_state(
    tmp_path: Path, prior_evidence: bool
):
    repo, _ = _make_release_repo(tmp_path)
    tag = "bes-v2026.07.18.1"
    evidence_path = repo / ".git" / "bes-release-state.json"
    before = b'{"previous":true}\r\n'
    if prior_evidence:
        evidence_path.write_bytes(before)
    tag_failure = """
function New-ReleaseTag {
    param([string]$RepoRoot, [string]$Tag, [string]$Commit)
    throw 'simulated tag creation failure'
}
"""

    result = _run_release_driver(tmp_path, repo, tag=tag, extra_script=tag_failure)

    assert result.returncode != 0
    normalized_stderr = " ".join(result.stderr.split()).lower()
    assert "was not created" in normalized_stderr
    assert "evidence was restored" in normalized_stderr
    assert "simulated tag creation failure" in normalized_stderr
    if prior_evidence:
        assert evidence_path.read_bytes() == before
    else:
        assert not evidence_path.exists()
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{tag}",
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.release
def test_push_failure_retains_local_tag_and_evidence_with_retry(tmp_path: Path):
    repo, remote = _make_release_repo(tmp_path)
    tag = "bes-v2026.07.18.1"
    push_failure = """
function Invoke-AtomicReleasePush {
    param([string]$RepoRoot, [string]$Tag, [string]$Remote)
    throw 'simulated atomic push failure'
}
"""

    result = _run_release_driver(tmp_path, repo, tag=tag, extra_script=push_failure)

    assert result.returncode != 0
    normalized_stderr = " ".join(result.stderr.split())
    assert "remote release did not complete" in normalized_stderr.lower()
    assert f"git push --atomic origin main refs/tags/{tag}" in normalized_stderr
    assert _git(repo, "cat-file", "-t", f"refs/tags/{tag}").stdout.strip() == "tag"
    assert (repo / ".git" / "bes-release-state.json").is_file()
    assert (
        _git(repo, "ls-remote", remote.as_posix(), f"refs/tags/{tag}").stdout.strip()
        == ""
    )


@pytest.mark.release
def test_global_reinstall_invokes_repo_script_by_absolute_path(tmp_path: Path):
    repo, _ = _make_release_repo(tmp_path)
    invocation_log = tmp_path / "reinstall-path.txt"
    reinstall = repo / "scripts" / "reinstall-global.ps1"
    reinstall.write_text(
        f"[IO.File]::WriteAllText({ps_quote(invocation_log)}, "
        "$MyInvocation.MyCommand.Path)\n",
        encoding="utf-8",
    )

    result = _run_release_function(f"Invoke-GlobalReinstall -RepoRoot {ps_quote(repo)}")

    assert result.returncode == 0, result.stderr
    assert Path(invocation_log.read_text(encoding="utf-8")).is_absolute()
    assert Path(invocation_log.read_text(encoding="utf-8")) == reinstall


def _write_release_evidence(
    repo: Path,
    *,
    tag: str,
    commit: str,
    extra: dict[str, object] | None = None,
) -> Path:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "tag": tag,
        "commit": commit,
        "verified_at": "2026-07-18T00:00:00Z",
        "source_manifest_sha256": "a" * 64,
    }
    if extra:
        evidence.update(extra)
    path = repo / ".git" / "bes-release-state.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _run_guard(repo: Path, pre_push_input: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT)],
        cwd=repo,
        input=pre_push_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=git_env_without_auto_maintenance(),
    )


def _make_release_tag(
    repo: Path, tag: str, *, annotated: bool = True
) -> tuple[str, str]:
    if annotated:
        _git(repo, "tag", "-a", tag, "-m", f"release {tag}")
    else:
        _git(repo, "tag", tag)
    object_id = _git(repo, "rev-parse", f"refs/tags/{tag}").stdout.strip()
    commit = _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}").stdout.strip()
    return object_id, commit


def test_guard_allows_verified_annotated_release_tag(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    tag = "bes-v2026.07.18.1"
    object_id, commit = _make_release_tag(repo, tag)
    _write_release_evidence(repo, tag=tag, commit=commit)
    line = f"refs/tags/{tag} {object_id} refs/tags/{tag} {ZERO_OID}\n"

    result = _run_guard(repo, line)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_guard_blocks_release_tag_refspec_rename(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    local_tag = "bes-v2026.07.18.1"
    remote_tag = "bes-v2026.07.18.2"
    object_id, commit = _make_release_tag(repo, local_tag)
    _write_release_evidence(repo, tag=local_tag, commit=commit)
    line = f"refs/tags/{local_tag} {object_id} refs/tags/{remote_tag} {ZERO_OID}\n"

    result = _run_guard(repo, line)

    assert result.returncode != 0
    assert "same local and remote reference" in result.stderr.lower()
    assert f"scripts/release.ps1 -Tag {remote_tag}" in result.stderr


def test_guard_validates_the_pushed_oid_instead_of_mutable_local_ref(
    tmp_path: Path,
):
    repo, _ = _make_repo_with_remote(tmp_path)
    tag = "bes-v2026.07.18.1"
    pushed_object_id, pushed_commit = _make_release_tag(repo, tag)
    _write_release_evidence(repo, tag=tag, commit=pushed_commit)

    (repo / "tracked.txt").write_text("moved tag\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "move local tag target")
    _git(repo, "tag", "-f", "-a", tag, "-m", f"moved {tag}")
    assert _git(repo, "rev-parse", f"refs/tags/{tag}").stdout.strip() != (
        pushed_object_id
    )

    line = f"refs/tags/{tag} {pushed_object_id} refs/tags/{tag} {ZERO_OID}\n"
    result = _run_guard(repo, line)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "evidence_kind",
    ["missing", "malformed-json", "extra-field", "tag-mismatch", "commit-mismatch"],
)
def test_guard_blocks_missing_malformed_or_mismatched_evidence(
    tmp_path: Path, evidence_kind: str
):
    repo, _ = _make_repo_with_remote(tmp_path)
    tag = "bes-v2026.07.18.1"
    object_id, commit = _make_release_tag(repo, tag)
    evidence_path = repo / ".git" / "bes-release-state.json"
    if evidence_kind == "malformed-json":
        evidence_path.write_text("{not-json", encoding="utf-8")
    elif evidence_kind == "extra-field":
        _write_release_evidence(
            repo, tag=tag, commit=commit, extra={"unexpected": True}
        )
    elif evidence_kind == "tag-mismatch":
        _write_release_evidence(repo, tag="bes-v2026.07.17.1", commit=commit)
    elif evidence_kind == "commit-mismatch":
        _write_release_evidence(repo, tag=tag, commit="b" * 40)
    line = f"refs/tags/{tag} {object_id} refs/tags/{tag} {ZERO_OID}\n"

    result = _run_guard(repo, line)

    assert result.returncode != 0
    assert tag in result.stderr
    assert f"scripts/release.ps1 -Tag {tag}" in result.stderr


def test_guard_blocks_lightweight_release_tag(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    tag = "bes-v2026.07.18.1"
    object_id, commit = _make_release_tag(repo, tag, annotated=False)
    _write_release_evidence(repo, tag=tag, commit=commit)
    line = f"refs/tags/{tag} {object_id} refs/tags/{tag} {ZERO_OID}\n"

    result = _run_guard(repo, line)

    assert result.returncode != 0
    assert "annotated" in result.stderr.lower()


def test_guard_blocks_update_to_existing_remote_release_tag(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    tag = "bes-v2026.07.18.1"
    object_id, commit = _make_release_tag(repo, tag)
    _write_release_evidence(repo, tag=tag, commit=commit)
    different_remote_oid = "c" * 40
    line = f"refs/tags/{tag} {object_id} refs/tags/{tag} {different_remote_oid}\n"

    result = _run_guard(repo, line)

    assert result.returncode != 0
    assert "immutable" in result.stderr.lower()


@pytest.mark.parametrize(
    "pre_push_input",
    [
        f"refs/heads/main {'a' * 40} refs/heads/main {ZERO_OID}\n",
        f"refs/tags/docs-v1 {'a' * 40} refs/tags/docs-v1 {ZERO_OID}\n",
        f"(delete) {ZERO_OID} refs/tags/bes-v2026.07.18.1 {'a' * 40}\n",
    ],
)
def test_guard_passes_through_ordinary_pushes_and_release_deletion(
    tmp_path: Path, pre_push_input: str
):
    repo, _ = _make_repo_with_remote(tmp_path)
    unreadable_marker = repo / ".git" / "bes-release-state.json"
    unreadable_marker.mkdir()
    before_status = _git(repo, "status", "--porcelain").stdout

    result = _run_guard(repo, pre_push_input)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "status", "--porcelain").stdout == before_status
    assert unreadable_marker.is_dir()


def test_pre_commit_config_installs_wrapper_but_not_raw_stdin_guard():
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )

    assert "pre-push" in config["default_install_hook_types"]
    local_hooks = [
        hook
        for repository in config["repos"]
        if repository["repo"] == "local"
        for hook in repository["hooks"]
    ]
    assert all("pre-push" not in hook.get("stages", []) for hook in local_hooks)


def _install_test_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    (repo / ".pre-commit-config.yaml").write_text(
        "default_install_hook_types: [pre-push]\nrepos: []\n",
        encoding="utf-8",
    )
    return run_pwsh_file(
        HOOK_INSTALLER_SCRIPT,
        [
            "-RepoRoot",
            str(repo),
            "-PythonProjectRoot",
            str(REPO_ROOT),
            "-GuardScriptPath",
            str(GUARD_SCRIPT),
        ],
        check=False,
        env=git_env_without_auto_maintenance(),
    )


@pytest.mark.release
def test_installer_composes_pre_commit_wrapper_and_raw_stdin_companion(
    tmp_path: Path,
):
    repo, remote = _make_repo_with_remote(tmp_path)

    install = _install_test_hook(repo)

    assert install.returncode == 0, install.stderr
    wrapper = repo / ".git" / "hooks" / "pre-push"
    companion = repo / ".git" / "hooks" / "pre-push.legacy"
    assert wrapper.is_file()
    assert companion.is_file()
    companion_text = companion.read_text(encoding="utf-8")
    assert "bes-release-pre-push" in companion_text
    assert "guard-release-tag.py" in companion_text
    assert "pytest" not in companion_text
    assert "reinstall" not in companion_text

    (repo / "tracked.txt").write_text("ordinary backup\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "ordinary backup")
    ordinary_push = _git(repo, "push", "origin", "main", check=False)
    assert ordinary_push.returncode == 0, ordinary_push.stderr

    (repo / "tracked.txt").write_text("release candidate\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "release candidate")
    tag = "bes-v2026.07.18.1"
    _make_release_tag(repo, tag)
    atomic_push = _git(
        repo,
        "push",
        "--atomic",
        "origin",
        "main",
        f"refs/tags/{tag}",
        check=False,
    )
    assert atomic_push.returncode != 0
    assert f"scripts/release.ps1 -Tag {tag}" in atomic_push.stderr
    assert (
        _git(repo, "ls-remote", remote.as_posix(), f"refs/tags/{tag}").stdout.strip()
        == ""
    )


def test_installer_refuses_to_replace_unrelated_legacy_hook(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    hooks = repo / ".git" / "hooks"
    legacy = hooks / "pre-push.legacy"
    legacy.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
    before = legacy.read_bytes()

    install = _install_test_hook(repo)

    assert install.returncode != 0
    assert "unrelated" in install.stderr.lower()
    assert legacy.read_bytes() == before


@pytest.mark.release
def test_installer_preserves_explicit_default_core_hooks_path(tmp_path: Path):
    repo, _ = _make_repo_with_remote(tmp_path)
    configured = str((repo / ".git" / "hooks").resolve())
    _git(repo, "config", "--local", "core.hooksPath", configured)

    install = _install_test_hook(repo)

    assert install.returncode == 0, install.stderr
    after = _git(
        repo, "config", "--local", "--get-all", "core.hooksPath"
    ).stdout.strip()
    assert after == configured
    assert (repo / ".git" / "hooks" / "pre-push").is_file()
    assert (repo / ".git" / "hooks" / "pre-push.legacy").is_file()


def test_archive_automation_is_scoped_to_skill_publication_only():
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    local_hooks = {
        hook["id"]: hook
        for repository in config["repos"]
        if repository["repo"] == "local"
        for hook in repository["hooks"]
    }

    assert "publish-cli-on-archive" not in local_hooks
    assert local_hooks["deploy-skill-to-ssot"]["stages"] == [
        "post-commit",
        "post-merge",
    ]
    assert not (REPO_ROOT / "scripts" / "publish-cli-on-archive.py").exists()


def test_operator_docs_distinguish_backup_release_and_skill_archive():
    """The operator-facing doc set must keep push, release, and skill archive apart.

    The set is three files, not two. The release procedure moved out of README and
    AGENTS.md into ``docs/global-install-and-release.md`` when those two were cut down
    to pointers; keeping the assertions on README+AGENTS alone would have forced the
    procedure to be duplicated back into them, which is what the pointer layout exists
    to prevent. Widening the corpus is only safe while both entry points still link to
    that file, so the link itself is asserted below — otherwise this guard would pass
    against a procedure no reader can reach from either entry point.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    release_doc_path = REPO_ROOT / "docs" / "global-install-and-release.md"
    release_doc = release_doc_path.read_text(encoding="utf-8")
    combined = readme + agents + release_doc

    assert "docs/global-install-and-release.md" in readme
    assert "docs/global-install-and-release.md" in agents

    assert "scripts\\release.ps1 -Tag bes-vYYYY.MM.DD.N" in combined
    assert "scripts\\install-release-pre-push.ps1" in combined
    assert "scripts\\reinstall-global.ps1" in combined
    # Case-folded: the claim under test is that the docs say a push is only a
    # backup, and "git push" is written lowercase in prose and uppercase at the
    # start of a sentence. Casing carries none of that meaning.
    assert "git push" in combined.lower() and "備份" in combined
    assert "archive" in combined and "skill" in combined
    assert "GitHub Actions" in combined
    assert "pyproject.toml" in combined
    assert "publish-cli-on-archive" not in agents


def test_release_script_invokes_launcher_full_route_only():
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    tests_function_start = script.index("function Invoke-ReleaseTests")

    assert "scripts\\invoke-pytest.ps1" in script
    assert "-Route Full" in script
    assert "canonical full pytest gate" in script.lower()
    assert "uv run --locked --exact pytest" not in script[tests_function_start:]
    assert "-Route Targeted" not in script
    assert "-Route Quick" not in script
    assert "-Route FullSequential" not in script
    tests_function_end = script.index("function Invoke-GlobalReinstall")
    tests_function_body = script[tests_function_start:tests_function_end]
    assert "$env:TEMP" not in tests_function_body
    assert "$env:TMP" not in tests_function_body


def test_release_tests_invoke_the_full_route_launcher_with_exact_arguments(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    log = tmp_path / "launcher-args.txt"
    (scripts / "invoke-pytest.ps1").write_text(
        f"[IO.File]::WriteAllText({ps_quote(log)}, ($args -join ' '))\nexit 0\n",
        encoding="utf-8",
    )

    result = _run_release_function(f"Invoke-ReleaseTests -RepoRoot {ps_quote(repo)}")

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").strip() == "-Route Full"


@pytest.mark.parametrize(
    ("launcher_exit_code", "expected_message_fragment"),
    [
        (1, "failed with exit code 1"),
        (17, "failed with exit code 17"),
        (18, "failed with exit code 18"),
    ],
)
def test_release_tests_throw_on_any_nonzero_launcher_exit_code(
    tmp_path: Path, launcher_exit_code: int, expected_message_fragment: str
):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "invoke-pytest.ps1").write_text(
        f"exit {launcher_exit_code}\n", encoding="utf-8"
    )

    result = _run_release_function(f"Invoke-ReleaseTests -RepoRoot {ps_quote(repo)}")

    assert result.returncode != 0
    assert expected_message_fragment in result.stderr


@pytest.mark.release
@pytest.mark.parametrize(
    ("launcher_exit_code", "expected_message_fragment"),
    [
        (1, "failed with exit code 1"),
        (17, "failed with exit code 17"),
        (18, "failed with exit code 18"),
    ],
)
def test_release_stops_before_reinstall_on_any_nonzero_launcher_exit(
    tmp_path: Path, launcher_exit_code: int, expected_message_fragment: str
):
    repo, _ = _make_release_repo(tmp_path)
    (repo / "scripts" / "invoke-pytest.ps1").write_text(
        f"exit {launcher_exit_code}\n", encoding="utf-8"
    )
    _git(repo, "add", "scripts")
    _git(repo, "commit", "-m", "stub canonical pytest launcher exit code")
    _git(repo, "push", "origin", "main")

    result = _run_release_driver(tmp_path, repo, override_tests=False)

    assert result.returncode != 0
    assert expected_message_fragment in result.stderr
    assert "Run full test suite" in result.stdout
    assert "Reinstall global bes" not in result.stdout
    assert "Verify installed snapshot" not in result.stdout
    assert "Write local release evidence" not in result.stdout
    assert "Create annotated release tag" not in result.stdout
    assert "Atomically push main and release tag" not in result.stdout
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/tags/bes-v2026.07.18.1",
            check=False,
        ).returncode
        != 0
    )


def test_disposable_repos_disable_git_auto_maintenance_for_direct_and_nested_git(
    tmp_path: Path,
):
    """See the companion check in tests/test_report_diff_coverage.py.

    The nested hop goes through this module's real PowerShell entry point
    rather than a bare ``pwsh`` call, so it proves the override survives the
    same descendant chain the release tests actually drive.
    """
    repo, remote = _make_repo_with_remote(tmp_path)

    direct = _git(
        repo, "config", "--show-origin", "--get", GIT_AUTO_MAINTENANCE_SETTING
    )
    assert git_config_origin_and_value(direct.stdout) == ("command line:", "false")

    nested = _run_release_function(
        f"git -C {ps_quote(repo)} config --show-origin "
        f"--get {GIT_AUTO_MAINTENANCE_SETTING}"
    )
    assert git_config_origin_and_value(nested.stdout) == ("command line:", "false"), (
        nested.stderr
    )

    # The bare remote is the third hop, and the only one the environment
    # cannot carry: `git push` starts receive-pack there with Git's
    # repository-scoped environment variables already cleared.
    remote_side = _git(
        remote, "config", "--local", "--get", GIT_AUTO_MAINTENANCE_SETTING
    )
    assert remote_side.stdout.strip() == "false"
