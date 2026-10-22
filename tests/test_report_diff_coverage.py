"""Behavior tests for scripts/report-diff-coverage.ps1 (#75).

Every test drives a real ``pwsh`` subprocess against the actual script,
against real throwaway git repositories built under ``tmp_path`` -- never an
in-process mock of the script's own logic. Two dependencies of the script are
replaced with small controllable PowerShell stubs, exactly the same style as
``tests/test_invoke_pytest.py``'s ``uv`` stub:

- ``scripts/invoke-pytest.ps1`` inside each fixture repo is a stub launcher
  (never the real one), so no test here ever runs the real pytest suite --
  it only ever writes a canned pytest-style summary line and, for the
  ``FullCoverage`` route, drops a caller-supplied coverage XML fixture into
  the run's derived staging directory.
- ``uv`` on PATH is a stub that answers ``--version`` and the
  ``diff-cover`` invocation the script makes twice per run, writing a
  caller-supplied JSON report instead of running the real tool.

This keeps every test here in the sub-second-to-few-second range while still
exercising the script's own git resolution, path-mismatch detection,
attribution logic, and artifact/manifest writing untouched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests._pwsh_support import (
    GIT_AUTO_MAINTENANCE_SETTING,
    disable_auto_maintenance_in_bare_repo,
    git_config_origin_and_value,
    git_env_without_auto_maintenance,
    run_pwsh_command,
    run_pwsh_file,
)

# ~170 s for the file (every test spawns pwsh and builds a git repo): Quick
# skips it, Full runs it (#12).
pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "report-diff-coverage.ps1"
REAL_ENV_HELPER = REPO_ROOT / "scripts" / "project-tool-environment.ps1"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """PowerShell colours its Format-List output; the codes break substring
    assertions on the field values."""
    return _ANSI.sub("", text)


# ---------------------------------------------------------------------------
# Stub launcher (replaces scripts/invoke-pytest.ps1 inside each fixture repo)
# ---------------------------------------------------------------------------

DEFAULT_JUNIT_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" tests="4">'
    '<testcase classname="tests.test_x" name="test_a"/>'
    '<testcase classname="tests.test_x" name="test_b"/>'
    '<testcase classname="tests.test_x" name="test_c"/>'
    '<testcase classname="tests.test_x" name="test_d"/>'
    "</testsuite></testsuites>"
)

STUB_LAUNCHER_PS1 = r"""
param(
    [string]$Route,
    [string]$CoverageRunId
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'project-tool-environment.ps1')

if ($env:STUB_LAUNCHER_DIRTY_FILE) {
    $driftPath = Join-Path $repoRoot $env:STUB_LAUNCHER_DIRTY_FILE
    Set-Content -LiteralPath $driftPath -Value 'unexpected drift' -Encoding utf8NoBOM
}

if ($env:STUB_LAUNCHER_SLEEP_SECONDS) {
    Start-Sleep -Seconds ([int]$env:STUB_LAUNCHER_SLEEP_SECONDS)
}

if ($Route -eq 'FullCoverage') {
    $summary = if ($env:STUB_LAUNCHER_COVERAGE_SUMMARY) {
        $env:STUB_LAUNCHER_COVERAGE_SUMMARY
    } else {
        '4 passed in 0.10s'
    }
    Write-Output $summary
    $artifactRoot = Get-ProjectRuntimeRoot -RepositoryRoot $repoRoot
    $staging = Join-Path (Join-Path $artifactRoot 'coverage') $CoverageRunId
    if (-not (Test-Path -LiteralPath $staging)) {
        New-Item -ItemType Directory -Path $staging -Force | Out-Null
    }
    if (-not $env:STUB_LAUNCHER_SKIP_XML) {
        $coverageXmlPath = Join-Path $staging 'coverage.xml'
        if ($env:STUB_LAUNCHER_MALFORMED_XML) {
            Set-Content -LiteralPath $coverageXmlPath -Value '<not-valid-xml' `
                -Encoding utf8NoBOM
        } elseif ($env:STUB_LAUNCHER_COVERAGE_XML_SOURCE) {
            Copy-Item -LiteralPath $env:STUB_LAUNCHER_COVERAGE_XML_SOURCE `
                -Destination $coverageXmlPath -Force
        }
    }
    if (-not $env:STUB_LAUNCHER_SKIP_COVERAGE_DATA) {
        # Simulates pytest-cov's raw COVERAGE_FILE data plus one xdist
        # worker's suffixed sibling, so tests can verify the manifest hashes
        # this retained data too (see report-diff-coverage.ps1's
        # artifactSha256 assembly).
        Set-Content -LiteralPath (Join-Path $staging '.coverage') `
            -Value 'stub coverage data' -Encoding utf8NoBOM
        Set-Content -LiteralPath (Join-Path $staging '.coverage.worker1') `
            -Value 'stub coverage data worker1' -Encoding utf8NoBOM
    }
    if (-not $env:STUB_LAUNCHER_SKIP_COVERAGE_JUNIT) {
        $junitXml = if ($env:STUB_LAUNCHER_COVERAGE_JUNIT_XML) {
            $env:STUB_LAUNCHER_COVERAGE_JUNIT_XML
        } else {
            $env:STUB_LAUNCHER_DEFAULT_JUNIT_XML
        }
        Set-Content -LiteralPath (Join-Path $staging 'junit.xml') -Value $junitXml `
            -Encoding utf8NoBOM -NoNewline
    }
    $exitCode = 0
    if ($env:STUB_LAUNCHER_COVERAGE_EXIT_CODE) {
        $exitCode = [int]$env:STUB_LAUNCHER_COVERAGE_EXIT_CODE
    }
    exit $exitCode
} elseif ($Route -eq 'Full') {
    $summary = if ($env:STUB_LAUNCHER_FULL_SUMMARY) {
        $env:STUB_LAUNCHER_FULL_SUMMARY
    } else {
        '4 passed in 0.09s'
    }
    Write-Output $summary
    if ($env:STUB_LAUNCHER_FULL_ROUTE_MARKER_FILE) {
        # Proves the orchestration script wires the same run ID it gave
        # FullCoverage into this second Full call (design.md > "Bind
        # collection evidence to the run that produced it").
        Add-Content -LiteralPath $env:STUB_LAUNCHER_FULL_ROUTE_MARKER_FILE `
            -Value "CoverageRunId=$CoverageRunId"
    }
    if ($CoverageRunId -and -not $env:STUB_LAUNCHER_SKIP_FULL_JUNIT) {
        # Mirrors the real launcher's version-3 route contract (#75 round
        # 4): Full with a run ID writes its junit XML into that SAME run's
        # own staging directory -- the second enumerated grant of
        # -CoverageRunId. A plain `Full` invocation with no run ID writes NO
        # junit report at all: that report has exactly one reader
        # (scripts/report-diff-coverage.ps1's comparison read), and that
        # reader always supplies a run ID, so there is nothing left to mirror
        # for the no-run-id case.
        $junitXml = if ($env:STUB_LAUNCHER_FULL_JUNIT_XML) {
            $env:STUB_LAUNCHER_FULL_JUNIT_XML
        } else {
            $env:STUB_LAUNCHER_DEFAULT_JUNIT_XML
        }
        $artifactRoot = Get-ProjectRuntimeRoot -RepositoryRoot $repoRoot
        $staging = Join-Path (Join-Path $artifactRoot 'coverage') $CoverageRunId
        if (-not (Test-Path -LiteralPath $staging)) {
            New-Item -ItemType Directory -Path $staging -Force | Out-Null
        }
        Set-Content -LiteralPath (Join-Path $staging 'full-junit.xml') `
            -Value $junitXml -Encoding utf8NoBOM -NoNewline
    }
    $exitCode = 0
    if ($env:STUB_LAUNCHER_FULL_EXIT_CODE) {
        $exitCode = [int]$env:STUB_LAUNCHER_FULL_EXIT_CODE
    }
    exit $exitCode
} else {
    [Console]::Error.WriteLine("stub launcher received unexpected -Route $Route")
    exit 90
}
"""

# ---------------------------------------------------------------------------
# Stub uv (answers `uv --version` and the two `uv run ... diff-cover ...`
# invocations Invoke-DiffCoverReport makes per run)
# ---------------------------------------------------------------------------

STUB_UV_PS1 = r"""
$argv = $args
if ($argv.Count -ge 1 -and $argv[0] -eq '--version') {
    Write-Output 'uv 0.9.99 (report-diff-coverage test stub)'
    exit 0
}
if ($argv.Count -ge 4 -and $argv[0] -eq 'run' -and $argv[3] -eq 'diff-cover') {
    $compareArg = @($argv | Where-Object { $_ -like '--compare-branch=*' })[0]
    $jsonArg = @($argv | Where-Object { $_ -like '--format=json:*' })[0]
    $compareRef = $compareArg.Substring('--compare-branch='.Length)
    $jsonPath = $jsonArg.Substring('--format=json:'.Length)

    $isR = $compareRef.EndsWith('/R')
    $isB = $compareRef.EndsWith('/B')

    $failOn = $env:STUB_DIFFCOVER_FAIL_ON
    $exitCode = 0
    $shouldFail = ($failOn -eq 'R' -and $isR) -or ($failOn -eq 'B' -and $isB) -or
        $failOn -eq 'BOTH'
    if ($failOn -and $shouldFail) {
        $exitCode = 5
    }

    $defaultJson = '{"total_percent_covered": 100.0, "total_num_lines": 4, ' +
        '"total_num_violations": 0, ' +
        '"src_stats": {"src/mailattic/sample.py": {}}}'
    $jsonText = $defaultJson
    if ($isR -and $env:STUB_DIFFCOVER_JSON_RT) {
        $jsonText = $env:STUB_DIFFCOVER_JSON_RT
    }
    if ($isB -and $env:STUB_DIFFCOVER_JSON_BT) {
        $jsonText = $env:STUB_DIFFCOVER_JSON_BT
    }

    if (-not $env:STUB_DIFFCOVER_SKIP_JSON) {
        Set-Content -LiteralPath $jsonPath -Value $jsonText -Encoding utf8NoBOM `
            -NoNewline
    }
    if ($env:STUB_DIFFCOVER_MARKER_FILE) {
        Add-Content -LiteralPath $env:STUB_DIFFCOVER_MARKER_FILE `
            -Value "CALL ref=$compareRef json=$jsonPath"
    }
    exit $exitCode
}
[Console]::Error.WriteLine("unexpected stub uv invocation: $($argv -join ' ')")
exit 99
"""

# One coverage.xml fixture, derived from the real schema read directly out of
# .project-runtime/coverage/probe1/coverage.xml (coverage.py's Cobertura-style
# output): <coverage> attributes, <sources>/<source>, <packages>/<package>,
# <classes>/<class filename=...>, <lines>/<line number= hits= [branch=
# condition-coverage= missing-branches=]>. Trimmed to one class, one partial
# branch line -- deliberately reused by every successful-run test, not just
# the partial-branch-focused one, since the script never validates these
# numbers against anything (only the per-line branch metadata is consumed by
# Get-PartialBranchLinesFromCoverageXml).
COVERAGE_XML_FIXTURE = """<?xml version="1.0" ?>
<coverage version="7.15.3" timestamp="1785748365565" lines-valid="4"
    lines-covered="3" line-rate="0.75" branches-valid="2" branches-covered="1"
    branch-rate="0.5" complexity="0">
    <sources>
        <source>PLACEHOLDER</source>
    </sources>
    <packages>
        <package name="mailattic" line-rate="0.75" branch-rate="0.5"
            complexity="0">
            <classes>
                <class name="sample.py" filename="mailattic/sample.py"
                    complexity="0" line-rate="0.75" branch-rate="0.5">
                    <methods/>
                    <lines>
                        <line number="1" hits="1"/>
                        <line number="2" hits="1"/>
                        <line number="3" hits="0" branch="true"
                            condition-coverage="50% (1/2)" missing-branches="4"/>
                        <line number="4" hits="0"/>
                    </lines>
                </class>
            </classes>
        </package>
    </packages>
</coverage>
"""


# ---------------------------------------------------------------------------
# Fixture-repo builders
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=git_env_without_auto_maintenance(),
    )


def _rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _init_bare_target_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A git repo with the stub launcher wired in, one initial commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Report Diff Coverage Test")
    _git(repo, "config", "user.email", "report-diff-coverage-test@example.invalid")
    (repo / ".gitignore").write_text(".project-runtime/\n", encoding="utf-8")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "project-tool-environment.ps1").write_bytes(
        REAL_ENV_HELPER.read_bytes()
    )
    (scripts_dir / "invoke-pytest.ps1").write_text(STUB_LAUNCHER_PS1, encoding="utf-8")
    src_dir = repo / "src" / "mailattic"
    src_dir.mkdir(parents=True)
    (src_dir / "sample.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _commit_sample_content(repo: Path, message: str, content: str) -> str:
    (repo / "src" / "mailattic" / "sample.py").write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _rev_parse(repo, "HEAD")


def _commit_touch(repo: Path, message: str) -> str:
    """A commit that touches src/ with unique, trivial content each time.

    Modifies sample.py (the same file the default stub diff-cover JSON's
    src_stats reports) rather than a separate file: the path-mismatch guard
    (report-diff-coverage.ps1) now compares the actual changed-vs-reported
    path SETS, not just a count, so a fixture that touches one file while the
    stubbed coverage report names a different one is a genuine path mismatch,
    not a harmless fixture detail.
    """
    marker = repo / "src" / "mailattic" / "sample.py"
    existing = marker.read_text(encoding="utf-8") if marker.exists() else ""
    marker.write_text(existing + f"# {message}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _rev_parse(repo, "HEAD")


def _commit_touch_two_files(repo: Path, message: str) -> str:
    """A commit that touches TWO distinct src/ files in one range.

    Used to distinguish a genuine path-SET mismatch (diff-cover recognizes
    only a subset of the changed files) from the total-miss case a bare
    ``src_stats.Count == 0`` check already caught.
    """
    (repo / "src" / "mailattic" / "second.py").write_text(
        "def g():\n    return 2\n", encoding="utf-8"
    )
    marker = repo / "src" / "mailattic" / "sample.py"
    existing = marker.read_text(encoding="utf-8") if marker.exists() else ""
    marker.write_text(existing + f"# {message}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _rev_parse(repo, "HEAD")


def _delete_and_commit(repo: Path, relative_path: str, message: str) -> str:
    """A commit whose ONLY content change is deleting an existing src/ file."""
    target = repo / relative_path
    target.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _rev_parse(repo, "HEAD")


def _create_dangling_junction(link_path: Path) -> None:
    """Make ``link_path`` a directory junction whose target does not exist.

    Its volume can never be established -- the same "unverifiable" mechanism
    tests/test_project_tool_environment.py already uses and verifies for a
    dangling reparse point.
    """
    missing_target = link_path.parent / f"missing-target-{uuid.uuid4().hex}"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(missing_target)],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def stub_uv_dir(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "stub-uv"
    stub_dir.mkdir()
    (stub_dir / "uv.ps1").write_text(STUB_UV_PS1, encoding="utf-8")
    return stub_dir


def _env(stub_uv_dir: Path, **overrides: str) -> dict[str, str]:
    # Every pwsh subprocess this module drives ultimately runs Git against a
    # disposable fixture repo, so the whole descendant tree inherits the
    # auto-maintenance suppression from here.
    env = git_env_without_auto_maintenance()
    env["PATH"] = str(stub_uv_dir) + os.pathsep + env.get("PATH", "")
    env["PATHEXT"] = ".PS1;" + env.get("PATHEXT", "")
    # The stub launcher's fallback junit XML content when neither
    # STUB_LAUNCHER_COVERAGE_JUNIT_XML nor STUB_LAUNCHER_FULL_JUNIT_XML is
    # given -- both routes default to the SAME population, so a test that
    # does not care about collection identity gets a match by default.
    env["STUB_LAUNCHER_DEFAULT_JUNIT_XML"] = DEFAULT_JUNIT_XML
    env.update(overrides)
    return env


def _run_script(
    args: list[str], *, env: dict[str, str], timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    # GitHub-hosted Windows runners occasionally need more than 45 seconds
    # for this nested PowerShell + git integration path under xdist load.
    return run_pwsh_file(SCRIPT, args, env=env, timeout=timeout)


def _base_args(
    repo: Path,
    *,
    change_name: str,
    r: str,
    b: str,
    t: str,
    declared: list[str],
) -> list[str]:
    return [
        "-RepositoryRoot",
        str(repo),
        "-ChangeName",
        change_name,
        "-R",
        r,
        "-B",
        b,
        "-T",
        t,
        "-DeclaredCommitSet",
        *declared,
    ]


def _coverage_run_dirs(repo: Path) -> list[Path]:
    coverage_root = repo / ".project-runtime" / "coverage"
    if not coverage_root.exists():
        return []
    return [p for p in coverage_root.iterdir() if p.is_dir()]


def _only_run_dir(repo: Path) -> Path:
    dirs = _coverage_run_dirs(repo)
    assert len(dirs) == 1, dirs
    return dirs[0]


def _read_manifest(staging_dir: Path) -> dict:
    return json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))


def _success_env(
    stub_uv_dir: Path, xml_source: Path, **overrides: str
) -> dict[str, str]:
    return _env(
        stub_uv_dir,
        STUB_LAUNCHER_COVERAGE_XML_SOURCE=str(xml_source),
        **overrides,
    )


@pytest.fixture
def coverage_xml_path(tmp_path: Path) -> Path:
    path = tmp_path / "coverage-fixture.xml"
    path.write_text(COVERAGE_XML_FIXTURE, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Dirty / drifting target repository fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_dirty_target_repo_at_start_fails_closed(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path, dirty_kind: str
):
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")
    target = repo / (
        "src/mailattic/sample.py" if dirty_kind == "tracked" else "untracked.txt"
    )
    target.write_text("dirty\n", encoding="utf-8")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "not clean at T" in result.stderr, result.stderr
    assert not (repo / ".project-runtime").exists(), (
        "a dirty-repo rejection must precede any runtime-artifact creation"
    )


def test_head_not_at_declared_t_fails_closed(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    real_head = _rev_parse(repo, "HEAD")
    wrong_t = "a" * 40  # a syntactically valid but wrong SHA, never HEAD

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(
            repo,
            change_name="c",
            r=real_head,
            b=real_head,
            t=wrong_t,
            declared=[real_head],
        ),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "does not resolve to a commit" in result.stderr, result.stderr


def test_repository_drift_during_run_fails_closed_with_no_completion_marker(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """The target repo must be re-verified clean AFTER the launcher runs.

    The stub launcher writes an untracked file into the repo root as a side
    effect (simulating something external touching the repo mid-run); the
    manifest is written before this check, but MANIFEST_COMPLETE never is --
    proving a killed-or-drifted run's manifest.json alone is not evidence.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_LAUNCHER_DIRTY_FILE="UNEXPECTED_DRIFT.txt",
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "drifted during the run" in result.stderr, result.stderr

    staging_dir = _only_run_dir(repo)
    assert (staging_dir / "manifest.json").exists()
    assert not (staging_dir / "MANIFEST_COMPLETE").exists(), (
        "a drifted run must never leave a completion marker behind"
    )


# ---------------------------------------------------------------------------
# 2. Run-directory identity and lifecycle
# ---------------------------------------------------------------------------


def test_two_runs_sharing_only_t_resolve_to_different_run_directories(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "change one")

    env = _success_env(stub_uv_dir, coverage_xml_path)

    result_a = _run_script(
        _base_args(repo, change_name="a", r=c0, b=c0, t=c1, declared=[c1]), env=env
    )
    assert result_a.returncode == 0, (result_a.stdout, result_a.stderr)

    result_b = _run_script(
        _base_args(repo, change_name="b", r=c1, b=c0, t=c1, declared=[c1]), env=env
    )
    assert result_b.returncode == 0, (result_b.stdout, result_b.stderr)

    run_dirs = _coverage_run_dirs(repo)
    assert len(run_dirs) == 2, run_dirs
    for staging_dir in run_dirs:
        assert (staging_dir / "MANIFEST_COMPLETE").exists()


def test_existing_staging_directory_is_refused_not_reused(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """The staging directory must be created fail-if-exists, never reused.

    NOTE: the run ID this script derives is seeded with a fresh
    ``[guid]::NewGuid()`` on every invocation (report-diff-coverage.ps1:447),
    so the exact "Test-Path already true" branch (line 452) cannot be forced
    from outside the process -- a real collision would require a GUID
    collision. This test instead forces the adjacent, still fail-closed
    outcome: the staging directory's *parent* segment already exists as a
    plain file, so directory creation itself fails; the script must still
    refuse to proceed rather than silently reusing/deleting anything. See
    the final report for this as a flagged testability gap, not a passing
    substitute for the exact-collision case.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    runtime_dir = repo / ".project-runtime"
    runtime_dir.mkdir()
    (runtime_dir / "coverage").write_text("blocker, not a directory", encoding="utf-8")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "fail-if-exists" in result.stderr, result.stderr


def test_killed_run_leaves_no_completion_marker(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """A process killed mid-run must leave no completion marker as evidence."""
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_LAUNCHER_SLEEP_SECONDS="10",
    )

    args = _base_args(
        repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]
    )
    proc = subprocess.Popen(
        [
            "pwsh",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        staging_dir: Path | None = None
        while time.monotonic() < deadline:
            dirs = _coverage_run_dirs(repo)
            if dirs and (dirs[0] / "run-descriptor.json").exists():
                staging_dir = dirs[0]
                break
            time.sleep(0.05)
        assert staging_dir is not None, (
            "the run never reached the point of writing its run descriptor"
        )
        assert proc.poll() is None, "the process finished before it could be killed"
    finally:
        proc.kill()
        proc.communicate(timeout=30)

    assert staging_dir is not None
    assert (staging_dir / "run-descriptor.json").exists()
    assert not (staging_dir / "manifest.json").exists()
    assert not (staging_dir / "MANIFEST_COMPLETE").exists(), (
        "a killed run must never be mistaken for a complete one"
    )


# ---------------------------------------------------------------------------
# 3. Commit-set attribution and labeling
# ---------------------------------------------------------------------------


def test_valid_single_change_range_attributes_correctly(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "the one change")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="single", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["commitSetsEqualBT"] is True
    assert manifest["commitSetsEqualRT"] is True
    assert manifest["attributionValidBT"] is True
    assert manifest["rtLabel"] == "single-change"
    assert manifest["pathMismatchBT"] is False


def test_ten_commit_local_stack_reports_cumulative_evidence_label(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    r_sha = _rev_parse(repo, "HEAD")
    shas = [r_sha]
    for i in range(10):
        shas.append(_commit_touch(repo, f"stack commit {i}"))
    b_sha = shas[-2]
    t_sha = shas[-1]

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(
            repo, change_name="stack", r=r_sha, b=b_sha, t=t_sha, declared=[t_sha]
        ),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    # The single declared commit matches the tight B...T range...
    assert manifest["commitSetsEqualBT"] is True
    assert manifest["attributionValidBT"] is True
    # ...but ten commits sit between R and T, so the wider range must be
    # labeled cumulative, never silently folded into "single-change".
    assert manifest["commitSetsEqualRT"] is False
    assert manifest["rtLabel"] == "cumulative-stack-evidence"
    assert len(manifest["computedCommitSetRT"]) == 10


def test_declared_computed_mismatch_invalidates_attribution_without_failing(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "the real change")
    wrong_declared = "b" * 40  # syntactically valid, but not the real commit

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(
            repo, change_name="mismatch", r=c0, b=c0, t=c1, declared=[wrong_declared]
        ),
        env=env,
    )
    # A mismatch is a report-only distinction (attribution invalidated,
    # never used as single-change evidence), not an operational failure --
    # per the script's own documented exit-code taxonomy at the top of the
    # file, only tool/XML/drift/path failures set a non-zero exit.
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["commitSetsEqualBT"] is False
    assert manifest["attributionValidBT"] is False
    assert manifest["declaredCommitSet"] == [wrong_declared]
    assert manifest["computedCommitSetBT"] == [c1]


def test_missing_declared_set_is_rejected(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        [
            "-RepositoryRoot",
            str(repo),
            "-ChangeName",
            "c",
            "-R",
            t_sha,
            "-B",
            t_sha,
            "-T",
            t_sha,
            "-DeclaredCommitSet",
            # A single space, not "": PowerShell unwraps a one-element array
            # for truthiness, and "" alone is falsy, which would instead hit
            # the earlier "provide exactly one of" branch. A non-empty-but-
            # blank string is truthy (so -DeclaredCommitSet is "provided"),
            # then trims/filters to nothing, landing on the actual empty-set
            # check this test targets.
            " ",
        ],
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "declared commit set is empty" in result.stderr, result.stderr
    assert not (repo / ".project-runtime").exists()


def test_empty_production_diff_is_reported_as_empty_not_full_coverage(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """B == T (nothing changed) must be labeled empty, sourced from git --
    never smoothed into "100% covered" just because diff-cover's own JSON
    (as stubbed here) reports 100% of zero files.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")

    empty_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 0, '
        '"total_num_violations": 0, "src_stats": {}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=empty_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c0, declared=[c0]),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["emptyProductionDiffBT"] is True
    assert manifest["changedProductionPathsBT"] == []
    # Because the diff is genuinely empty, the empty src_stats above must
    # NOT be read as a path mismatch.
    assert manifest["pathMismatchBT"] is False

    # The human-facing summary must say so too. A real run of this script
    # printed "DiffCoverPercentBT : 100" for an empty diff -- correct in the
    # manifest, and exactly the wrong thing to show someone scanning a
    # terminal, who reads it as "every changed line is covered" when nothing
    # was measured at all. Asserting only on the manifest is what let that
    # through the first time.
    #
    # Assert on the BT field specifically. A bare "is the phrase anywhere in
    # stdout" check does NOT discriminate: the RT field prints the same phrase,
    # so reverting only the BT branch left such a check green. Verified by
    # mutation.
    collapsed = re.sub(r"\s+", " ", _strip_ansi(result.stdout))
    assert "DiffCoverPercentBT : empty (no production changes)" in collapsed, collapsed
    assert "DiffCoverPercentBT : 100" not in collapsed, collapsed


def test_path_mismatch_is_surfaced_not_smoothed_into_everything_covered(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """KNOWN RED, documenting a real defect -- see the final test-run report.

    git genuinely reports a changed src/ file for B...T, but diff-cover's
    (stubbed) report claims zero files with coverage information -- the exact
    ambiguity the pathMismatchBT check exists to catch. This currently fails
    (script exits 0 instead of 1) because ``ConvertFrom-Json '{}'``
    (PowerShell's own JSON deserializer, not this test's stub) produces a
    PSCustomObject carrying one phantom property whose Name is the empty
    string, so ``$srcStatsKeysBT.Count`` (report-diff-coverage.ps1:603) is 1,
    never 0, for a genuinely empty ``src_stats: {}`` object -- permanently
    disabling this guard for the one input shape it exists to catch. Kept as
    a real (not weakened) assertion per this test-writer's evidence-before-
    assertion rule: never adjust an assertion to hide a defect.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "a real src/ change")

    mismatched_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 0, '
        '"total_num_violations": 0, "src_stats": {}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=mismatched_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Path mismatch" in result.stderr, result.stderr
    assert (
        'as "nothing to cover"' in result.stderr or "nothing to cover" in result.stderr
    )


def test_partial_path_mismatch_when_diffcover_covers_a_subset_of_changed_files(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Two src/ files change; diff-cover's (stubbed) report covers only one.

    A count-only check (``src_stats.Count == 0``) misses this on purpose: the
    count here is 1, never 0, so a naive re-check of the total-miss guard
    alone would wrongly call this a match. The guard must compare the actual
    SETS of changed-vs-reported paths, not just whether the count is zero.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch_two_files(repo, "touch two src files")

    partial_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 2, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/sample.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=partial_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Path mismatch" in result.stderr, result.stderr
    assert "second.py" in result.stderr, result.stderr


def test_matching_multi_file_path_set_does_not_trigger_path_mismatch(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Negative control for the partial-path-mismatch guard above: when
    diff-cover's reported file set exactly matches the changed set (even
    across multiple files), no mismatch must be raised."""
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch_two_files(repo, "touch two src files")

    full_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 4, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/sample.py": {}, '
        '"src/mailattic/second.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=full_json,
        STUB_DIFFCOVER_JSON_RT=full_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["pathMismatchBT"] is False


def test_pure_deletion_is_not_flagged_as_path_mismatch(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """A range whose ONLY content change is deleting a src/ file must not be
    flagged as a path mismatch: diff-cover can never report a deleted file
    in src_stats (the target side has no lines to measure), so comparing
    against the FULL changed-path list (rather than the measurable subset
    that still exists at T) would misjudge every deletion as the exact
    defect this guard exists to catch.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _delete_and_commit(repo, "src/mailattic/sample.py", "delete sample.py")

    empty_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 0, '
        '"total_num_violations": 0, "src_stats": {}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=empty_json,
        STUB_DIFFCOVER_JSON_RT=empty_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["pathMismatchBT"] is False
    assert manifest["changedProductionPathsBT"] == ["src/mailattic/sample.py"]
    assert manifest["measurableProductionPathsBT"] == []


def test_deletion_alongside_measurable_edit_is_not_flagged_as_path_mismatch(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Required negative control: a change that deletes one production file
    AND makes an ordinary, measurable edit to another must still pass --
    the deletion must not drag down the unrelated, correctly-reported edit.
    """
    repo = _init_bare_target_repo(tmp_path)
    second = repo / "src" / "mailattic" / "second.py"
    second.write_text("def g():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add second module")
    c_setup = _rev_parse(repo, "HEAD")

    (repo / "src" / "mailattic" / "sample.py").unlink()
    second.write_text("def g():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete sample.py, edit second.py")
    c1 = _rev_parse(repo, "HEAD")

    only_second_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 1, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/second.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=only_second_json,
        STUB_DIFFCOVER_JSON_RT=only_second_json,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c_setup, b=c_setup, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["pathMismatchBT"] is False
    assert set(manifest["changedProductionPathsBT"]) == {
        "src/mailattic/sample.py",
        "src/mailattic/second.py",
    }
    assert manifest["measurableProductionPathsBT"] == ["src/mailattic/second.py"]


def test_malformed_report_with_null_src_stats_is_rejected_even_when_diff_is_empty(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """diff-cover can exit 0 while writing syntactically valid JSON whose
    src_stats field is null. Previously this collapsed into the same
    treatment as a legitimate empty src_stats: {}, and because the
    path-mismatch guard only runs when the range's changed-path set is
    non-empty, a malformed report for a genuinely-empty range slipped
    through entirely -- writing MANIFEST_COMPLETE with null totals. The
    shape check must reject this UNCONDITIONALLY, regardless of whether the
    diff itself is empty.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    malformed_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 0, '
        '"total_num_violations": 0, "src_stats": null}'
    )
    env = _success_env(
        stub_uv_dir, coverage_xml_path, STUB_DIFFCOVER_JSON_BT=malformed_json
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "malformed report" in result.stderr, result.stderr
    assert "src_stats" in result.stderr, result.stderr
    # Distinguishable from the legitimate-empty-diff and path-mismatch
    # messages -- neither phrase should appear here, or a broken shape check
    # could hide behind one of the OTHER rules that share this failure
    # surface instead of actually firing itself.
    assert "nothing to cover" not in result.stderr, result.stderr
    assert "Path mismatch" not in result.stderr, result.stderr
    for staging_dir in _coverage_run_dirs(repo):
        assert not (staging_dir / "MANIFEST_COMPLETE").exists()


def test_malformed_report_missing_numeric_field_is_rejected(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "a change")

    malformed_json = (
        '{"total_percent_covered": 100.0, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/sample.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir, coverage_xml_path, STUB_DIFFCOVER_JSON_BT=malformed_json
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "malformed report" in result.stderr, result.stderr
    assert "total_num_lines" in result.stderr, result.stderr
    for staging_dir in _coverage_run_dirs(repo):
        assert not (staging_dir / "MANIFEST_COMPLETE").exists()


def test_unsafe_artifact_root_is_rejected_before_any_io(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """If the default .project-runtime is unverifiable (here, a dangling
    directory junction), the rejection must happen BEFORE any staging
    directory or run-descriptor.json is created -- not only later, after the
    launcher's own equivalent (later) check would otherwise catch a run
    already known to be doomed.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    junction = repo / ".project-runtime"
    _create_dangling_junction(junction)

    env = _success_env(stub_uv_dir, coverage_xml_path)
    try:
        result = _run_script(
            _base_args(
                repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]
            ),
            env=env,
        )
    finally:
        # `rmdir` removes the link without following it into the (nonexistent)
        # target; a `rmtree` here would be a no-op at best.
        junction.rmdir()

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Unsafe coverage artifact path" in result.stderr, result.stderr


def test_collection_identity_mismatch_with_equal_totals_and_no_failures_is_rejected(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Same terminal totals (3 passed, 1 skipped) in both runs, and NEITHER
    run has any failures -- an aggregate-counts-only comparison (or a
    FAILED/ERROR-identity-only comparison) would call this a match. Only a
    full junit-reported (nodeid, outcome) identity comparison catches a skip
    that moved from one test to another while nothing ever failed.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    instrumented_junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="4">'
        '<testcase classname="tests.test_x" name="test_a"/>'
        '<testcase classname="tests.test_x" name="test_b">'
        '<skipped message="skip"/></testcase>'
        '<testcase classname="tests.test_x" name="test_c"/>'
        '<testcase classname="tests.test_x" name="test_d"/>'
        "</testsuite></testsuites>"
    )
    uninstrumented_junit = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="pytest" tests="4">'
        '<testcase classname="tests.test_x" name="test_a"/>'
        '<testcase classname="tests.test_x" name="test_b"/>'
        '<testcase classname="tests.test_x" name="test_c">'
        '<skipped message="skip"/></testcase>'
        '<testcase classname="tests.test_x" name="test_d"/>'
        "</testsuite></testsuites>"
    )

    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_LAUNCHER_COVERAGE_SUMMARY="3 passed, 1 skipped in 0.10s",
        STUB_LAUNCHER_FULL_SUMMARY="3 passed, 1 skipped in 0.09s",
        STUB_LAUNCHER_COVERAGE_JUNIT_XML=instrumented_junit,
        STUB_LAUNCHER_FULL_JUNIT_XML=uninstrumented_junit,
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "collection/subtest identity mismatch" in result.stderr, result.stderr
    for staging_dir in _coverage_run_dirs(repo):
        assert not (staging_dir / "MANIFEST_COMPLETE").exists()


def test_collection_identity_match_with_identical_populations_still_succeeds(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Negative control: identical totals AND identical junit-reported test
    identity in both runs must still be accepted as valid evidence."""
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["collectionIdentityMatch"] is True
    assert manifest["instrumentedTestIdentityCount"] == 4
    assert manifest["uninstrumentedTestIdentityCount"] == 4


def test_junit_reports_are_hashed_in_the_completed_manifest(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """The instrumented and uninstrumented junit XML reports are run
    artifacts too and must be hashed like every other artifact -- the same
    integrity guarantee the .coverage* files already get."""
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    hashed_names = set(manifest["artifactSha256"].keys())
    assert "junit.xml" in hashed_names, hashed_names
    assert "full-junit.xml" in hashed_names, hashed_names


# ---------------------------------------------------------------------------
# Task 5.1/5.2 (#75 round 4) -- collection evidence bound to the run that
# produced it (design.md > "Bind collection evidence to the run that
# produced it"). Both the FullCoverage and the uninstrumented Full call must
# be bound to the SAME run ID, and the Full call's evidence must never live
# at a name shared with any other run.
# ---------------------------------------------------------------------------


def test_full_route_receives_the_same_run_id_full_coverage_used(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """The second (uninstrumented) Full call must be bound to the SAME run
    as the first FullCoverage call -- otherwise its junit evidence could not
    legitimately be compared against the instrumented run's collection
    identity at all.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")
    marker = tmp_path / "full-route-marker.txt"

    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_LAUNCHER_FULL_ROUTE_MARKER_FILE=str(marker),
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    run_dir = _only_run_dir(repo)
    assert marker.exists(), "Full route was never invoked with a -CoverageRunId marker"
    assert marker.read_text(encoding="utf-8").strip() == f"CoverageRunId={run_dir.name}"


def test_full_route_junit_lands_only_in_this_runs_staging_directory(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Provenance: the uninstrumented comparison run's evidence must be
    bound to THIS run's own staging directory and must never also appear at
    the old shared fixed-name leaf -- proving no copy-from-elsewhere step
    remains and nothing is written to a name another run could overwrite.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    run_dir = _only_run_dir(repo)
    assert (run_dir / "full-junit.xml").exists()

    # No junit report is ever written into the shared "pytest" leaf: version
    # 3 of the launcher's route contract (#75 round 4) removed that
    # fixed-name write path from Full entirely -- there is no longer a
    # shared name for evidence to leak to.
    pytest_leaf = repo / ".project-runtime" / "pytest"
    assert not pytest_leaf.exists() or not list(pytest_leaf.glob("*.xml")), (
        "the uninstrumented Full run must never write a junit report into "
        "the shared 'pytest' leaf when it is bound to a comparison run"
    )


def test_missing_full_route_junit_in_staging_directory_fails_closed(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """If the launcher's Full route fails to leave evidence in THIS run's
    own staging directory, the script must fail closed with a message
    naming that run's directory -- never silently fall back to reading a
    shared name elsewhere (there is no such fallback path left to read).
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(
        stub_uv_dir, coverage_xml_path, STUB_LAUNCHER_SKIP_FULL_JUNIT="1"
    )
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "junit XML report was not produced" in result.stderr, result.stderr
    run_dir = _only_run_dir(repo)
    # The error must name THIS run's own staging directory (its run ID is
    # embedded verbatim in the expected path) -- not the old shared
    # "pytest" leaf -- proving the script reads directly from the directory
    # it already owns rather than any fixed shared location.
    assert run_dir.name in result.stderr, result.stderr
    # The old shared "pytest" leaf is never named -- there is no fallback
    # path left to point at.
    assert str(repo / ".project-runtime" / "pytest") not in result.stderr, result.stderr
    for staging_dir in _coverage_run_dirs(repo):
        assert not (staging_dir / "MANIFEST_COMPLETE").exists()


def test_launcher_capture_files_never_land_in_system_temp_when_killed_mid_run(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """A process killed mid-run must never have stranded its launcher
    stdout/stderr capture files in the system temp directory.

    Redirects the system temp directory to an isolated, per-test scratch
    directory (via TEMP/TMP) so this is deterministic rather than racing real
    system temp clutter: [System.IO.Path]::GetTempFileName() resolves
    against exactly those two environment variables. See
    test_killed_run_leaves_no_completion_marker for the completion-marker
    half of the same kill scenario.
    """
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")
    fake_system_temp = tmp_path / "fake-system-temp"
    fake_system_temp.mkdir()

    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_LAUNCHER_SLEEP_SECONDS="10",
        TEMP=str(fake_system_temp),
        TMP=str(fake_system_temp),
    )

    args = _base_args(
        repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]
    )
    proc = subprocess.Popen(
        [
            "pwsh",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        staging_dir: Path | None = None
        while time.monotonic() < deadline:
            dirs = _coverage_run_dirs(repo)
            if dirs and (dirs[0] / "run-descriptor.json").exists():
                staging_dir = dirs[0]
                break
            time.sleep(0.05)
        assert staging_dir is not None, (
            "the run never reached the point of writing its run descriptor"
        )
        assert proc.poll() is None, "the process finished before it could be killed"
    finally:
        proc.kill()
        proc.communicate(timeout=30)

    assert staging_dir is not None
    leftover = list(fake_system_temp.iterdir())
    assert leftover == [], (
        f"launcher capture files were stranded in system temp after a kill: {leftover}"
    )


def test_completed_manifest_hashes_the_coverage_data_files(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """The retained ``.coverage*`` data file(s) pytest-cov leaves behind must
    be part of the completed manifest's hash list -- hashing only the
    derived XML/reports means mutating the underlying data afterward is
    undetectable."""
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    hashed_names = set(manifest["artifactSha256"].keys())
    assert ".coverage" in hashed_names, hashed_names
    assert ".coverage.worker1" in hashed_names, hashed_names


def test_merge_stack_ambiguity_invalidates_attribution_but_keeps_cumulative_report(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    base = _rev_parse(repo, "HEAD")

    _git(repo, "switch", "-c", "feature")
    (repo / "src" / "mailattic" / "feature.py").write_text(
        "FEATURE = 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature commit")
    feature_sha = _rev_parse(repo, "HEAD")

    _git(repo, "switch", "main")
    _commit_touch(repo, "main-line commit")

    _git(repo, "merge", "feature", "--no-ff", "-m", "merge feature into main")
    merge_sha = _rev_parse(repo, "HEAD")

    # The merge combines the feature branch's feature.py with the main-line
    # sample.py touch, so B...T (and R...T, identical range here since
    # r == b) genuinely spans two src/ files -- the stubbed diff-cover report
    # must reflect both, or the (correct) path-mismatch guard would reject
    # this run for an unrelated reason and mask the attribution assertion
    # this test actually targets.
    two_file_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 4, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/sample.py": {}, '
        '"src/mailattic/feature.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=two_file_json,
        STUB_DIFFCOVER_JSON_RT=two_file_json,
    )
    result = _run_script(
        _base_args(
            repo,
            change_name="merge-ambiguity",
            r=base,
            b=base,
            t=merge_sha,
            declared=[feature_sha],
        ),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["attributionValidBT"] is False
    assert manifest["rtLabel"] == "cumulative-stack-evidence"
    # The cumulative report itself must still be produced and populated --
    # invalid single-change attribution does not blank out the informational
    # R...T evidence.
    assert manifest["diffCoverReportRT"]["totalPercentCovered"] is not None
    assert manifest["diffCoverReportBT"]["totalPercentCovered"] is not None


_VOLATILE_MANIFEST_KEYS = {
    "runId",
    "runDirectoryKey",
    "changeName",
    "commands",
    "artifactSha256",
}


def test_moved_symbolic_origin_ref_does_not_change_the_recorded_result(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    repo = _init_bare_target_repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    # receive-pack runs inside the remote, past the environment override.
    disable_auto_maintenance_in_bare_repo(remote)
    _git(repo, "remote", "add", "origin", str(remote))

    r_sha = _rev_parse(repo, "HEAD")
    _commit_touch(repo, "penultimate commit")
    b_sha = _rev_parse(repo, "HEAD")
    t_sha = _commit_touch(repo, "final commit")
    _git(repo, "push", "-u", "origin", "main")

    env = _success_env(stub_uv_dir, coverage_xml_path)
    result_before = _run_script(
        _base_args(
            repo, change_name="before-move", r=r_sha, b=b_sha, t=t_sha, declared=[t_sha]
        ),
        env=env,
    )
    assert result_before.returncode == 0, (result_before.stdout, result_before.stderr)
    manifest_before = _read_manifest(_only_run_dir(repo))

    # Move origin/main out from under the captured SHAs, entirely
    # out-of-band (a second clone pushing further commits), then fetch so
    # the local remote-tracking ref itself visibly moves too.
    external_clone = tmp_path / "external-push"
    _git(tmp_path, "clone", str(remote), str(external_clone))
    (external_clone / "extra.txt").write_text(
        "someone else's commit\n", encoding="utf-8"
    )
    _git(external_clone, "config", "user.name", "Someone Else")
    _git(external_clone, "config", "user.email", "someone-else@example.invalid")
    _git(external_clone, "add", "-A")
    _git(external_clone, "commit", "-m", "an unrelated push that moves origin/main")
    _git(external_clone, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    assert _rev_parse(repo, "refs/remotes/origin/main") != t_sha

    result_after = _run_script(
        _base_args(
            repo, change_name="after-move", r=r_sha, b=b_sha, t=t_sha, declared=[t_sha]
        ),
        env=env,
    )
    assert result_after.returncode == 0, (result_after.stdout, result_after.stderr)

    run_dirs = sorted(_coverage_run_dirs(repo), key=lambda p: p.stat().st_mtime)
    assert len(run_dirs) == 2
    manifest_after = _read_manifest(run_dirs[-1])
    assert manifest_after == manifest_before or {
        k: v for k, v in manifest_after.items() if k not in _VOLATILE_MANIFEST_KEYS
    } == {k: v for k, v in manifest_before.items() if k not in _VOLATILE_MANIFEST_KEYS}


# ---------------------------------------------------------------------------
# 4. Coverage XML parsing and report-only exit semantics
# ---------------------------------------------------------------------------


def test_changed_partial_branches_are_parsed_from_xml_not_diffcover_percentage(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """KNOWN RED, documenting a real defect -- see the final test-run report.

    Exactly one line intersects a partial branch here on purpose (the
    smallest case that exercises the guard at all). It currently fails
    because ``$changedPartialBranchesBT = Get-ChangedPartialBranches ...``
    (report-diff-coverage.ps1:627) captures a function's pipeline output
    without an ``@()`` at the *call site*; PowerShell only guarantees array
    typing when the array-subexpression wraps the call itself (an internal
    ``return @(...)`` inside the function is not enough) -- so when the
    function's result has exactly one element, ``$changedPartialBranchesBT``
    becomes that bare object, and ``ConvertTo-Json`` then emits a JSON object
    instead of a one-element JSON array. Any real consumer of manifest.json
    expecting a stable array shape breaks on this exact, unremarkable input.
    Kept as a real (not weakened) assertion -- see this file's docstring
    policy on RED tests documenting defects.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    # New-side lines 1-4 land exactly on the fixture's partial-branch line 3
    # (condition-coverage 50%, missing-branches "4").
    c1 = _commit_sample_content(
        repo,
        "introduce a branch",
        "def f():\n    if True:\n        return 1\n    return 2\n",
    )

    # diff-cover itself (stubbed) claims 100% -- if the script inferred
    # partial-branch detail from diff-cover's percentage instead of the raw
    # XML, this would wrongly show zero changed partial branches.
    full_json = (
        '{"total_percent_covered": 100.0, "total_num_lines": 4, '
        '"total_num_violations": 0, '
        '"src_stats": {"src/mailattic/sample.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=full_json,
        STUB_DIFFCOVER_JSON_RT=full_json,
    )
    result = _run_script(
        _base_args(repo, change_name="branches", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["diffCoverReportBT"]["totalPercentCovered"] == 100.0
    changed = manifest["changedPartialBranchesBT"]
    assert len(changed) == 1, changed
    assert changed[0]["file"] == "src/mailattic/sample.py"
    assert changed[0]["line"] == 3
    assert changed[0]["missingBranches"] == "4"


@pytest.mark.parametrize(
    ("overrides", "expected_stderr_fragment"),
    [
        ({"STUB_LAUNCHER_COVERAGE_EXIT_CODE": "3"}, "FullCoverage exited with code 3"),
        ({"STUB_LAUNCHER_FULL_EXIT_CODE": "4"}, "-Route Full (uninstrumented"),
        ({"STUB_LAUNCHER_SKIP_XML": "1"}, "Expected coverage XML was not produced"),
        ({"STUB_LAUNCHER_MALFORMED_XML": "1"}, "Coverage XML is malformed"),
        (
            {"STUB_LAUNCHER_FULL_SUMMARY": "3 passed in 0.05s"},
            "disagree on terminal outcome counts",
        ),
        ({"STUB_DIFFCOVER_FAIL_ON": "B"}, "diff-cover failed for the B...T range"),
    ],
    ids=[
        "full-coverage-route-exit-nonzero",
        "full-route-exit-nonzero",
        "coverage-xml-missing",
        "coverage-xml-malformed",
        "instrumented-uninstrumented-mismatch",
        "diff-cover-tool-failure",
    ],
)
def test_operational_failures_exit_non_zero_and_leave_no_completion_marker(
    tmp_path: Path,
    stub_uv_dir: Path,
    coverage_xml_path: Path,
    overrides: dict[str, str],
    expected_stderr_fragment: str,
):
    repo = _init_bare_target_repo(tmp_path)
    t_sha = _rev_parse(repo, "HEAD")

    env = _success_env(stub_uv_dir, coverage_xml_path, **overrides)
    result = _run_script(
        _base_args(repo, change_name="c", r=t_sha, b=t_sha, t=t_sha, declared=[t_sha]),
        env=env,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert expected_stderr_fragment in result.stderr, result.stderr
    for staging_dir in _coverage_run_dirs(repo):
        assert not (staging_dir / "MANIFEST_COMPLETE").exists()


def test_low_coverage_percentage_alone_still_exits_success(
    tmp_path: Path, stub_uv_dir: Path, coverage_xml_path: Path
):
    """Report-only contract: a low (even zero) diff-cover percentage must
    never itself be treated as a failure -- contrast with the operational
    failures above (tool/XML/drift), which always exit non-zero.
    """
    repo = _init_bare_target_repo(tmp_path)
    c0 = _rev_parse(repo, "HEAD")
    c1 = _commit_touch(repo, "a change with terrible coverage")

    low_json = (
        '{"total_percent_covered": 0.0, "total_num_lines": 40, '
        '"total_num_violations": 40, '
        '"src_stats": {"src/mailattic/sample.py": {}}}'
    )
    env = _success_env(
        stub_uv_dir,
        coverage_xml_path,
        STUB_DIFFCOVER_JSON_BT=low_json,
        STUB_DIFFCOVER_JSON_RT=low_json,
    )
    result = _run_script(
        _base_args(repo, change_name="low", r=c0, b=c0, t=c1, declared=[c1]),
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = _read_manifest(_only_run_dir(repo))
    assert manifest["diffCoverReportBT"]["totalPercentCovered"] == 0.0
    assert (_only_run_dir(repo) / "MANIFEST_COMPLETE").exists()


def test_disposable_repos_disable_git_auto_maintenance_for_direct_and_nested_git(
    tmp_path: Path, stub_uv_dir: Path
):
    """Every fixture repo here is discarded, so its automatic maintenance is
    pure cost: Git forks ``git maintenance run --auto`` after commits in repos
    no test ever asserts maintenance behavior on.

    Both hops are asserted because they fail independently. Git launched
    directly by this module reads the override from the environment
    ``_git`` builds; Git launched *below* the ``pwsh`` subprocesses this
    module drives reads it only if ``_env`` carries it too. Wiring one and
    forgetting the other still removes some of the children, which is exactly
    the partial fix that would otherwise pass unnoticed.
    """
    repo = _init_bare_target_repo(tmp_path)

    direct = _git(
        repo, "config", "--show-origin", "--get", GIT_AUTO_MAINTENANCE_SETTING
    )
    assert git_config_origin_and_value(direct.stdout) == ("command line:", "false")

    nested = run_pwsh_command(
        f"git config --show-origin --get {GIT_AUTO_MAINTENANCE_SETTING}",
        cwd=repo,
        env=_env(stub_uv_dir),
    )
    assert git_config_origin_and_value(nested.stdout) == ("command line:", "false"), (
        nested.stderr
    )
