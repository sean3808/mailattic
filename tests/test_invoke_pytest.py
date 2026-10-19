"""Behavior tests for the canonical pytest launcher (scripts/invoke-pytest.ps1).

Every test drives a real ``pwsh`` subprocess against the actual launcher
script. ``uv`` is replaced on ``PATH`` with a small PowerShell stub that
records its working directory, its exact argument vector, and (on request)
the managed environment variables and a Python ``tempfile`` probe -- so
these tests assert on genuinely observed child-process state, never on
in-process mocks of the launcher's own logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._pwsh_support import (
    ps_quote,
    pwsh_file_argv,
    run_pwsh_command,
    run_pwsh_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "invoke-pytest.ps1"
HELPER_SCRIPT = REPO_ROOT / "scripts" / "project-tool-environment.ps1"

MANAGED_LEAF_BY_VARIABLE = {
    "TEMP": "python-temp",
    "TMP": "python-temp",
    "TMPDIR": "python-temp",
    "UV_CACHE_DIR": "uv-cache",
    "XDG_CACHE_HOME": "xdg-cache",
    "PRE_COMMIT_HOME": "pre-commit",
}

ALL_LEAF_NAMES = (
    "python-temp",
    "pytest",
    "ledger-collection",
    "uv-cache",
    "xdg-cache",
    "pre-commit",
    "locks",
)

STUB_UV_PS1 = r"""$marker = $env:STUB_UV_MARKER_FILE
if ($marker) {
    Add-Content -LiteralPath $marker -Value "CWD=$((Get-Location).Path)"
    Add-Content -LiteralPath $marker -Value "VAR_TEMP=$env:TEMP"
    Add-Content -LiteralPath $marker -Value "VAR_TMP=$env:TMP"
    Add-Content -LiteralPath $marker -Value "VAR_TMPDIR=$env:TMPDIR"
    Add-Content -LiteralPath $marker -Value "VAR_UV_CACHE_DIR=$env:UV_CACHE_DIR"
    Add-Content -LiteralPath $marker -Value "VAR_XDG_CACHE_HOME=$env:XDG_CACHE_HOME"
    Add-Content -LiteralPath $marker -Value "VAR_PRE_COMMIT_HOME=$env:PRE_COMMIT_HOME"
    foreach ($a in $args) {
        Add-Content -LiteralPath $marker -Value "ARG=$a"
    }
    if ($env:STUB_UV_TEMPFILE_PROBE) {
        $probe = & $env:STUB_UV_PYTHON -c "import tempfile; print(tempfile.mkdtemp())"
        Add-Content -LiteralPath $marker -Value "TEMPFILE_PROBE=$probe"
    }
}

if ($env:STUB_UV_SLEEP_SECONDS) {
    Start-Sleep -Seconds ([int]$env:STUB_UV_SLEEP_SECONDS)
}

# Hold the launcher's lock until the test explicitly releases it, so a
# contention test never gambles on the second process's pwsh cold start
# finishing within a fixed sleep. The 60s cap is a hang guard only.
if ($env:STUB_UV_HOLD_UNTIL_FILE) {
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-Path -LiteralPath $env:STUB_UV_HOLD_UNTIL_FILE)) {
        if ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Milliseconds 50
    }
}

$stubExitCode = 0
if ($env:STUB_UV_EXIT_CODE) {
    $stubExitCode = [int]$env:STUB_UV_EXIT_CODE
}
exit $stubExitCode
"""


@pytest.fixture
def stub_uv_dir(tmp_path: Path) -> Path:
    """A PATH-prependable directory containing a stub ``uv.ps1``.

    A ``.ps1`` stub (rather than a ``.cmd``/batch stub) is required for
    faithful argument-vector capture: cmd.exe's batch parameter parsing
    treats an unquoted ``=`` as a token separator like a space, so an
    argument such as ``--dist=load`` silently splits into two positional
    parameters (`--dist` and `load`) by the time a batch script's ``%1``
    would even see it. PowerShell's own ExternalScript invocation carries
    ``$args`` through as the real string array with no such re-tokenizing
    (verified directly against a throwaway probe script before writing this
    fixture). Every test env must add ``.PS1`` to ``PATHEXT`` (see
    ``_base_env``) so bare ``uv`` resolves to this script; PATH directories
    are still searched in order first, so this stub wins over any real
    ``uv.exe`` installed elsewhere on PATH.
    """
    stub_dir = tmp_path / "stub-uv"
    stub_dir.mkdir()
    (stub_dir / "uv.ps1").write_text(STUB_UV_PS1, encoding="utf-8")
    return stub_dir


def _base_env(stub_uv_dir: Path, marker: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(stub_uv_dir) + os.pathsep + env.get("PATH", "")
    # Required so bare `uv` resolves to the stub's uv.ps1 -- see the
    # stub_uv_dir fixture docstring for why this must be a .ps1, not a .cmd.
    env["PATHEXT"] = ".PS1;" + env.get("PATHEXT", "")
    env["STUB_UV_MARKER_FILE"] = str(marker)
    env.update(overrides)
    return env


def _run_launcher(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return run_pwsh_file(LAUNCHER, args, env=env, cwd=cwd, timeout=timeout)


def _parse_marker(path: Path) -> dict[str, object]:
    cwd: str | None = None
    args: list[str] = []
    variables: dict[str, str] = {}
    extra: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CWD="):
            cwd = line[len("CWD=") :]
        elif line.startswith("ARG="):
            args.append(line[len("ARG=") :])
        elif line.startswith("VAR_"):
            key, _, value = line[len("VAR_") :].partition("=")
            variables[key] = value
        elif "=" in line:
            key, _, value = line.partition("=")
            extra[key] = value
    return {"cwd": cwd, "args": args, "variables": variables, "extra": extra}


def _query_runtime_leaves(runtime_root: Path, tmp_path: Path) -> dict[str, Path]:
    """Ask the shared helper (not a Python reimplementation) for the leaves
    that a launcher invocation with the same -RuntimeRoot would resolve.
    """
    driver = tmp_path / "query-leaves.ps1"
    body = f"""
$ErrorActionPreference = 'Stop'
. {ps_quote(HELPER_SCRIPT)}
$normalized = Get-ProjectRuntimeRoot `
    -RepositoryRoot {ps_quote(tmp_path)} `
    -RuntimeRoot {ps_quote(runtime_root)}
Write-Output "ROOT=$normalized"
$leaves = Get-ProjectRuntimeLeaves -RuntimeRoot $normalized
foreach ($key in $leaves.Keys) {{
    Write-Output "LEAF_$key=$($leaves[$key])"
}}
"""
    driver.write_text(body, encoding="utf-8")
    result = run_pwsh_file(driver, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    leaves: dict[str, Path] = {}
    for line in result.stdout.splitlines():
        if line.startswith("LEAF_"):
            key, _, value = line[len("LEAF_") :].partition("=")
            leaves[key] = Path(value)
    return leaves


# ---------------------------------------------------------------------------
# Task 2.1 -- serialization
# ---------------------------------------------------------------------------


def test_second_concurrent_launch_exits_17_without_invoking_uv_or_mutating_output(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker_a = tmp_path / "marker-a.txt"
    marker_b = tmp_path / "marker-b.txt"

    release_a = tmp_path / "release-a.txt"

    env_a = _base_env(
        stub_uv_dir,
        marker_a,
        STUB_UV_HOLD_UNTIL_FILE=str(release_a),
        STUB_UV_EXIT_CODE="0",
    )
    proc_a = subprocess.Popen(
        pwsh_file_argv(
            LAUNCHER,
            [
                "-Route",
                "Targeted",
                "-Target",
                "dummy-target",
                "-RuntimeRoot",
                str(runtime_root),
            ],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env_a,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker_a.exists():
            time.sleep(0.05)
        assert marker_a.exists(), (
            "process A never invoked the stub uv (lock never reached)"
        )
        assert proc_a.poll() is None, (
            "process A exited before B could contend for the lock"
        )

        env_b = _base_env(stub_uv_dir, marker_b)
        result_b = _run_launcher(
            [
                "-Route",
                "Targeted",
                "-Target",
                "dummy-target",
                "-RuntimeRoot",
                str(runtime_root),
            ],
            env=env_b,
        )

        # Order matters: if A released the lock early (slow host, B's pwsh cold
        # start outran A's sleep), B legitimately gets the lock and returns 0.
        # That is "no contention observed", not "lock broken" -- assert it first
        # so the failure message tells the two apart.
        assert proc_a.poll() is None, (
            "process A finished before B's contended attempt completed; "
            "the test did not actually observe concurrent contention"
        )
        assert result_b.returncode == 17, (result_b.stdout, result_b.stderr)
        assert not marker_b.exists(), "process B invoked uv despite lock contention"

        release_a.write_text("release", encoding="utf-8")
        stdout_a, stderr_a = proc_a.communicate(timeout=30)
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
            proc_a.communicate(timeout=10)

    assert proc_a.returncode == 0, (stdout_a, stderr_a)
    assert marker_a.exists()


def test_full_coverage_staging_validation_runs_after_lock_is_held(
    tmp_path: Path, stub_uv_dir: Path
):
    """Ordering guarantee (CHANGE task 1.2): the coverage staging-directory
    shape/descriptor validation must execute while the project pytest lock
    is held, immediately before pytest launches -- never before lock
    acquisition, or a directory that changes in the window between an early
    check and launch would go unnoticed.

    Proven the same way the plain lock-contention test above proves lock
    ordering: process A holds the lock via a slow Targeted run against the
    same runtime root. Process B then issues a FullCoverage invocation whose
    staging directory does not exist at all -- a condition that, on its own,
    the launcher reports as exit 19 (see
    test_full_coverage_missing_staging_directory_exits_19). If the
    shape/descriptor validation ran before lock acquisition (the pre-fix
    ordering), process B would independently observe that exit-19 failure
    regardless of A's held lock. Observing exit 17 instead proves lock
    acquisition is checked first, and the staging validation is never
    reached at all while the lock is contended.
    """
    runtime_root = tmp_path / "runtime"
    marker_a = tmp_path / "marker-a.txt"
    marker_b = tmp_path / "marker-b.txt"

    release_a = tmp_path / "release-a.txt"

    env_a = _base_env(
        stub_uv_dir,
        marker_a,
        STUB_UV_HOLD_UNTIL_FILE=str(release_a),
        STUB_UV_EXIT_CODE="0",
    )
    proc_a = subprocess.Popen(
        pwsh_file_argv(
            LAUNCHER,
            [
                "-Route",
                "Targeted",
                "-Target",
                "dummy-target",
                "-RuntimeRoot",
                str(runtime_root),
            ],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env_a,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker_a.exists():
            time.sleep(0.05)
        assert marker_a.exists(), (
            "process A never invoked the stub uv (lock never reached)"
        )
        assert proc_a.poll() is None, (
            "process A exited before B could contend for the lock"
        )

        env_b = _base_env(stub_uv_dir, marker_b)
        result_b = _run_launcher(
            [
                "-Route",
                "FullCoverage",
                "-CoverageRunId",
                "run-b",
                "-RuntimeRoot",
                str(runtime_root),
            ],
            env=env_b,
        )

        # Same ordering caveat as the plain lock-contention test above: assert
        # A is still running first, so a false pass (B legitimately winning
        # the lock because A finished early) is never mistaken for "ordering
        # broken".
        assert proc_a.poll() is None, (
            "process A finished before B's contended attempt completed; "
            "the test did not actually observe concurrent contention"
        )
        assert result_b.returncode == 17, (result_b.stdout, result_b.stderr)
        assert not marker_b.exists(), "process B invoked uv despite lock contention"

        release_a.write_text("release", encoding="utf-8")
        stdout_a, stderr_a = proc_a.communicate(timeout=30)
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
            proc_a.communicate(timeout=10)

    assert proc_a.returncode == 0, (stdout_a, stderr_a)
    assert marker_a.exists()


def test_stale_lock_file_without_open_handle_does_not_block_next_run(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    locks_dir = runtime_root / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / "pytest.lock").write_bytes(
        b"stale lock file, no process holds a handle"
    )

    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists(), "a stale unlocked lock file blocked the next run"


# ---------------------------------------------------------------------------
# Task 2.2 -- route argument contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "extra_cli_args", "expected_pytest_args"),
    [
        (
            "Targeted",
            # Bare trailing tokens, no explicit -Target flag at all. See
            # test_targeted_accepts_all_three_target_invocation_forms below
            # for the other two accepted -Target invocation shapes.
            ["tests/test_a.py::test_one", "tests/test_b.py"],
            ["-q", "tests/test_a.py::test_one", "tests/test_b.py"],
        ),
        (
            "Quick",
            [],
            ["-n", "3", "--dist=load", "-q", "-m", "not slow and not release", "tests"],
        ),
        ("Full", [], ["-n", "3", "--dist=load", "-q", "tests"]),
        ("FullSequential", [], ["-n", "0", "-q", "tests"]),
    ],
)
def test_route_passes_exact_argument_vector(
    tmp_path: Path,
    stub_uv_dir: Path,
    route: str,
    extra_cli_args: list[str],
    expected_pytest_args: list[str],
):
    # A bare Full invocation (no -CoverageRunId) carries NO --junitxml
    # element at all (version 3 of the route contract, #75 round 4): that
    # report exists solely for scripts/report-diff-coverage.ps1's own
    # comparison read, which always supplies a run ID now, so an unbound
    # Full invocation has no reader to produce one for. See
    # test_full_with_run_id_writes_junit_into_its_own_staging_directory_only
    # below for the -CoverageRunId case.
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        ["-Route", route, *extra_cli_args, "-RuntimeRoot", str(runtime_root)],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    expected_full_args = [
        "run",
        "--locked",
        "--exact",
        "pytest",
        *expected_pytest_args,
        # Appended only because -RuntimeRoot is given; a run without the flag
        # keeps the route vector above byte-for-byte. Rationale lives on the
        # launcher's own -RuntimeRoot documentation.
        f"--basetemp={runtime_root / 'pytest'}",
    ]
    assert parsed["args"] == expected_full_args


@pytest.mark.parametrize(
    ("cli_args_after_route", "expected_targets"),
    [
        (
            ["-Target", "tests/test_a.py::test_one"],
            ["tests/test_a.py::test_one"],
        ),
        (
            ["-Target", "tests/test_a.py::test_one", "tests/test_b.py"],
            ["tests/test_a.py::test_one", "tests/test_b.py"],
        ),
        (
            ["tests/test_a.py::test_one", "tests/test_b.py"],
            ["tests/test_a.py::test_one", "tests/test_b.py"],
        ),
    ],
    ids=[
        "single-target-flag-value",
        "one-flag-multiple-values",
        "bare-trailing-tokens",
    ],
)
def test_targeted_accepts_all_three_target_invocation_forms(
    tmp_path: Path,
    stub_uv_dir: Path,
    cli_args_after_route: list[str],
    expected_targets: list[str],
):
    """Regression test for the -RuntimeRoot positional-stealing defect.

    -RuntimeRoot is passed *after* the target tokens here on purpose: with
    the old declarative parameter binding, a bare trailing token intended
    for -Target was captured by -RuntimeRoot instead (because -RuntimeRoot
    had not yet received a named value), leaving -Target empty and the
    Targeted route rejected. All three accepted invocation shapes must
    resolve -RuntimeRoot to the value actually given here, and must collect
    every target token into $Target -- never into $RuntimeRoot.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            *cli_args_after_route,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    expected_full_args = [
        "run",
        "--locked",
        "--exact",
        "pytest",
        "-q",
        *expected_targets,
        f"--basetemp={runtime_root / 'pytest'}",
    ]
    assert parsed["args"] == expected_full_args
    # The strongest possible evidence that -RuntimeRoot was not stolen by a
    # target token: the runtime root directory actually got created there.
    assert runtime_root.exists(), (
        "runtime root was never created at the -RuntimeRoot value given; "
        "a target token may have been bound to -RuntimeRoot instead"
    )


def test_exit_code_propagates_from_child(tmp_path: Path, stub_uv_dir: Path):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker, STUB_UV_EXIT_CODE="7")

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 7, (result.stdout, result.stderr)
    assert marker.exists()


def test_targeted_without_target_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        ["-Route", "Targeted", "-RuntimeRoot", str(runtime_root)],
        env=env,
    )

    assert result.returncode != 0
    assert not marker.exists(), "uv was invoked despite a missing required -Target"
    assert "-Target" in result.stderr
    assert not runtime_root.exists(), (
        "validation failure must precede any runtime-leaf creation"
    )


@pytest.mark.parametrize("route", ["Quick", "Full", "FullSequential"])
@pytest.mark.parametrize(
    "target_args",
    [
        ["-Target", "tests/test_should_not_run.py"],
        # The bare marker with no value: the parser skips the token itself,
        # so without tracking that the flag was seen at all this would be
        # silently ignored and the route would launch.
        ["-Target"],
    ],
    ids=["with-value", "bare-marker"],
)
def test_non_targeted_with_target_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path, route: str, target_args: list[str]
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        ["-Route", route, *target_args, "-RuntimeRoot", str(runtime_root)],
        env=env,
    )

    assert result.returncode != 0
    assert not marker.exists(), (
        "uv was invoked despite an invalid -Target for a non-Targeted route"
    )
    assert "-Target" in result.stderr
    assert not runtime_root.exists()


@pytest.mark.parametrize(
    "unknown_token",
    # `-p` is here on purpose: it is an ambiguous prefix of the PowerShell
    # common parameters -ProgressAction/-PipelineVariable, so any advanced
    # form of this script would have it rejected by pwsh's own binder before
    # the launcher's guard ran. It reaching exit 2 is the evidence that the
    # rejection contract really is universal.
    # `@options.txt` is here because the locked pytest expands `@file` as a
    # response file (verified directly against pytest 9.0.3), so forwarding
    # it as a target would let its contents reach pytest unchecked.
    ["--basetemp=C:\\pytest-tmp", "--collect-only", "-p", "-", "@options.txt"],
)
def test_unknown_dash_token_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path, unknown_token: str
):
    """A dash-prefixed token the launcher does not recognize must not reach pytest.

    Forwarding it as a target would let any caller smuggle arbitrary pytest
    options past the isolation contract -- `--basetemp=C:\\...` being the
    concrete escape: it redirects pytest's tmp tree back onto the system
    drive, which is exactly what this launcher exists to prevent.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "tests/test_a.py",
            unknown_token,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), f"uv was invoked despite unknown token {unknown_token}"
    # Not the whole token: pwsh -File splits an argument such as
    # `--basetemp=C:\pytest-tmp` at the colon, so the script body only ever
    # sees the leading fragment.
    assert "Unknown option" in result.stderr
    assert not runtime_root.exists(), (
        "validation failure must precede any runtime-leaf creation"
    )


@pytest.mark.parametrize(
    "cli_args",
    [
        # Every case must be followed by a VALID occurrence of the same flag
        # (supplied by the test body for -RuntimeRoot, inline here for
        # -Route), so that removing the value-slot guard makes the launcher
        # actually start rather than merely fail for a different reason.
        ["-Route", "-p", "-Route", "Targeted", "tests/test_a.py"],
        ["-Route", "Targeted", "tests/test_a.py", "-RuntimeRoot", "-p"],
        # The nastiest shape: a later valid -RuntimeRoot would overwrite the
        # swallowed one, so without a value-slot check this command would
        # launch with the unknown token simply gone.
        ["-Route", "Targeted", "tests/test_a.py", "-RuntimeRoot", "--collect-only"],
    ],
    ids=["route-value", "runtimeroot-value", "runtimeroot-value-overwritten-later"],
)
def test_dash_token_in_a_value_slot_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path, cli_args: list[str]
):
    """A flag's value slot must not swallow a dash-prefixed token.

    Consuming it as a value is the one remaining route by which an
    unrecognized option could vanish without ever reaching the unknown-option
    guard -- silently, when a later occurrence of the same flag overwrites it.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher([*cli_args, "-RuntimeRoot", str(runtime_root)], env=env)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a dash token in a value slot"
    assert not runtime_root.exists(), (
        "validation failure must precede any runtime-leaf creation"
    )


def test_repository_root_cwd_is_used_regardless_of_caller_cwd(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    caller_cwd = tmp_path / "elsewhere"
    caller_cwd.mkdir()
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
        cwd=caller_cwd,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    assert parsed["cwd"] is not None
    assert Path(str(parsed["cwd"])).resolve() == REPO_ROOT.resolve()


# ---------------------------------------------------------------------------
# Task 2.3 -- temp/cache isolation
# ---------------------------------------------------------------------------


def test_managed_environment_and_leaves_have_no_aliasing_below_runtime_root(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    leaves = _query_runtime_leaves(runtime_root, tmp_path)
    assert set(leaves) == set(ALL_LEAF_NAMES)

    parsed = _parse_marker(marker)
    variables = parsed["variables"]
    assert isinstance(variables, dict)
    for var_name, leaf_name in MANAGED_LEAF_BY_VARIABLE.items():
        assert var_name in variables, f"child process never observed {var_name}"
        observed = Path(variables[var_name]).resolve()
        expected = leaves[leaf_name].resolve()
        assert observed == expected, (
            f"{var_name} resolved to {observed}, expected {expected}"
        )

    resolved_root = runtime_root.resolve()
    all_leaf_paths = [leaves[name].resolve() for name in ALL_LEAF_NAMES]
    assert len(set(all_leaf_paths)) == len(all_leaf_paths), (
        "leaf paths are not pairwise distinct"
    )
    for leaf_path in all_leaf_paths:
        assert resolved_root in leaf_path.parents or resolved_root == leaf_path


@pytest.mark.parametrize(
    ("route_args", "expected_pytest_args"),
    [
        (["-Route", "Targeted", "tests/test_a.py"], ["-q", "tests/test_a.py"]),
        (
            ["-Route", "Quick"],
            ["-n", "3", "--dist=load", "-q", "-m", "not slow and not release", "tests"],
        ),
        (["-Route", "Full"], ["-n", "3", "--dist=load", "-q", "tests"]),
        (["-Route", "FullSequential"], ["-n", "0", "-q", "tests"]),
    ],
    ids=["Targeted", "Quick", "Full", "FullSequential"],
)
def test_route_without_runtime_root_passes_no_basetemp_argument(
    tmp_path: Path,
    stub_uv_dir: Path,
    route_args: list[str],
    expected_pytest_args: list[str],
):
    """The basetemp argument must appear *only* under -RuntimeRoot.

    Every other launcher test supplies -RuntimeRoot, because a default-root
    run would contend for the real pytest lock held by the suite running this
    test. So the launcher and its helper are copied into a scratch tree whose
    repository root is that tree: the same scripts, the same default-root code
    path, its own lock. Without this case an implementation that appended
    basetemp unconditionally would satisfy every other assertion here.
    """
    scratch_repo = tmp_path / "scratch-repo"
    scratch_scripts = scratch_repo / "scripts"
    scratch_scripts.mkdir(parents=True)
    for source in (LAUNCHER, HELPER_SCRIPT):
        (scratch_scripts / source.name).write_bytes(source.read_bytes())

    # A bare Full invocation carries no --junitxml element at all (version 3
    # of the route contract, #75 round 4) -- the parametrize table's "Full"
    # entry above already reflects that, so no special-casing is needed here.
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = run_pwsh_file(
        scratch_scripts / LAUNCHER.name, route_args, env=env, timeout=30
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    assert parsed["args"] == [
        "run",
        "--locked",
        "--exact",
        "pytest",
        *expected_pytest_args,
    ]


def test_runtime_root_override_moves_pytest_basetemp_to_its_own_leaf(
    tmp_path: Path, stub_uv_dir: Path
):
    """-RuntimeRoot must relocate pytest's own temporary tree as well.

    The override exists for a checkout whose repository volume is unsuitable.
    Every other managed path follows an environment variable the helper sets,
    but basetemp is a static repo-relative ``addopts`` value -- so without an
    explicit argument the override would silently leave pytest's ``tmp_path``
    writes, the largest of the seven, back on the repository volume.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    leaves = _query_runtime_leaves(runtime_root, tmp_path)
    parsed = _parse_marker(marker)
    args = parsed["args"]
    assert isinstance(args, list)
    basetemp_args = [a for a in args if a.startswith("--basetemp=")]
    assert len(basetemp_args) == 1, args
    observed = Path(basetemp_args[0][len("--basetemp=") :]).resolve()
    assert observed == leaves["pytest"].resolve()


def test_python_tempfile_probe_writes_below_the_runtime_root(
    tmp_path: Path, stub_uv_dir: Path
):
    """Python's own tempfile module must land inside the managed runtime root.

    The launcher redirects TEMP/TMP/TMPDIR, and this proves the redirection
    reaches `tempfile` rather than only the environment. Phrased as containment
    rather than as "not on the system drive": the latter says nothing about the
    launcher on a workstation whose runtime root is on the system drive by
    construction, and would have passed on the old two-disk machine no matter
    where the probe actually landed.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(
        stub_uv_dir,
        marker,
        STUB_UV_TEMPFILE_PROBE="1",
        STUB_UV_PYTHON=sys.executable,
    )

    result = _run_launcher(
        [
            "-Route",
            "Targeted",
            "-Target",
            "dummy-target",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    parsed = _parse_marker(marker)
    extra = parsed["extra"]
    assert isinstance(extra, dict)
    probe_path = extra.get("TEMPFILE_PROBE")
    assert probe_path, (parsed, result.stdout, result.stderr)
    resolved_probe = Path(probe_path).resolve()

    expected_leaf = (runtime_root / "python-temp").resolve()
    assert expected_leaf in resolved_probe.parents, (
        f"tempfile probe landed at {resolved_probe}, outside the managed "
        f"python-temp leaf {expected_leaf}"
    )

    resolved_root = runtime_root.resolve()
    assert resolved_root in resolved_probe.parents or resolved_root == resolved_probe


# ---------------------------------------------------------------------------
# FullCoverage route -- argument contract and staging-directory fail-closed
# checks
# ---------------------------------------------------------------------------


def _write_run_descriptor(
    staging_dir: Path,
    *,
    run_id: str,
    lease_namespace: str | None = None,
    owner_process_id: int | None = None,
) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    descriptor = {
        "runId": run_id,
        "leaseNamespace": lease_namespace if lease_namespace is not None else run_id,
        "ownerProcessId": (
            owner_process_id if owner_process_id is not None else os.getpid()
        ),
    }
    (staging_dir / "run-descriptor.json").write_text(
        json.dumps(descriptor), encoding="utf-8"
    )


def _dead_process_id() -> int:
    """A PID that has already exited, for the dead-owner descriptor case."""
    proc = subprocess.Popen(["pwsh", "-NoProfile", "-Command", "exit 0"])
    proc.wait(timeout=30)
    return proc.pid


def _create_directory_junction(link_path: Path, target_path: Path) -> None:
    result = run_pwsh_command(
        "New-Item -ItemType Junction -Path "
        f"{ps_quote(link_path)} -Target {ps_quote(target_path)} | Out-Null",
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_full_coverage_without_run_id_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        ["-Route", "FullCoverage", "-RuntimeRoot", str(runtime_root)], env=env
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a missing -CoverageRunId"
    assert "-CoverageRunId is required" in result.stderr
    assert not runtime_root.exists(), (
        "validation failure must precede any runtime-leaf creation"
    )


@pytest.mark.parametrize(
    ("route", "extra_args"),
    [
        ("Targeted", ["tests/test_a.py"]),
        ("Quick", []),
        ("FullSequential", []),
    ],
    ids=["Targeted", "Quick", "FullSequential"],
)
def test_coverage_run_id_is_rejected_for_non_coverage_routes(
    tmp_path: Path, stub_uv_dir: Path, route: str, extra_args: list[str]
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            route,
            *extra_args,
            "-CoverageRunId",
            "some-run",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), (
        f"uv was invoked despite -CoverageRunId on non-coverage route {route}"
    )
    assert "-CoverageRunId is only valid together with -Route FullCoverage" in (
        result.stderr
    )
    assert not runtime_root.exists()


@pytest.mark.parametrize(
    ("run_id", "expected_message_fragment"),
    [
        ("../evil", "is invalid"),
        ("a/b", "is invalid"),
        # A value beginning with '-' is caught by the earlier, more general
        # value-slot guard (see test_dash_token_in_a_value_slot_is_rejected_
        # before_launch above) before the run-ID regex is ever evaluated --
        # still exit 2, but via a different message.
        ("-lead", "values may not start with '-'"),
        # An empty string is falsy in PowerShell, so it is caught by the
        # "is required" check rather than the regex -- still exit 2, still a
        # rejected spelling, but via a third distinct message.
        ("", "-CoverageRunId is required"),
        ("a\\b", "is invalid"),
    ],
    ids=["dot-dot-slash", "forward-slash", "leading-dash", "empty", "backslash"],
)
def test_invalid_coverage_run_id_spelling_is_rejected_before_launch(
    tmp_path: Path,
    stub_uv_dir: Path,
    run_id: str,
    expected_message_fragment: str,
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            run_id,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), f"uv was invoked despite invalid run ID {run_id!r}"
    assert expected_message_fragment in result.stderr, result.stderr
    assert not runtime_root.exists()


def test_full_coverage_missing_staging_directory_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a missing staging directory"
    assert "does not exist" in result.stderr


def test_full_coverage_empty_staging_directory_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    staging.mkdir(parents=True)
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a descriptor-less directory"
    assert "must contain only the whitelisted entries" in result.stderr, result.stderr


def test_full_coverage_staging_directory_with_extra_file_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    """Distinct from the empty-directory case: a correct descriptor plus one
    stray file must still be refused. A guard that merely checks "directory
    is not empty" would wrongly accept this -- the actual rule is "no entry
    other than the descriptor".
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    (staging / "stray.txt").write_text("unexpected", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a stray file beside the run descriptor"
    )
    assert "must contain only the whitelisted entries" in result.stderr, result.stderr


@pytest.mark.parametrize(
    "extra_files",
    [
        [],
        ["launcher-stdout.log", "launcher-stderr.log"],
        ["launcher-stdout.log"],
        ["launcher-stderr.log"],
    ],
    ids=["descriptor-only", "both-logs", "stdout-only", "stderr-only"],
)
def test_full_coverage_staging_directory_whitelist_boundary_is_accepted(
    tmp_path: Path, stub_uv_dir: Path, extra_files: list[str]
):
    """Whitelist-boundary acceptance rows (spec example table, issue #87):
    the calling entry point (scripts/report-diff-coverage.ps1) necessarily
    creates its own stdout/stderr redirection log files before this launcher
    process even starts, so the instrumented route's shape check must accept
    the run descriptor plus zero, one, or both of those two exact file
    names -- and then proceed to the descriptor-identity checks, which pass
    here because the descriptor matches the invocation.

    ``descriptor-only`` already passed under the old sole-entry gate; the
    three log-bearing rows are the ones that MUST fail red against it (see
    the CHANGE tasks.md task 2.1 red-evidence requirement -- verified by
    temporarily reverting the whitelist change in scripts/invoke-pytest.ps1,
    observing this parametrization fail for every id except descriptor-only,
    then restoring the fix).
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    for name in extra_files:
        (staging / name).write_text("log content", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists(), (
        "uv was not invoked despite a whitelist-accepted staging directory "
        f"(extra files: {extra_files})"
    )


@pytest.mark.parametrize(
    "extra_file_name",
    ["launcher-other.log", "launcher-stdout.log.bak"],
    ids=["near-miss-other-log", "near-miss-bak-suffix"],
)
def test_full_coverage_staging_directory_rejects_names_outside_the_whitelist(
    tmp_path: Path, stub_uv_dir: Path, extra_file_name: str
):
    """The whitelist matches exact file names, not patterns: a near-miss
    entry that merely resembles a whitelisted log name (an unrelated
    ``launcher-*.log``, or a ``launcher-stdout.log`` with an extra suffix)
    must still be refused. A pattern/wildcard implementation would wrongly
    accept either of these and go red here.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    (staging / extra_file_name).write_text("unexpected", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a near-miss entry outside the whitelist: "
        f"{extra_file_name}"
    )
    assert "must contain only the whitelisted entries" in result.stderr, result.stderr


@pytest.mark.parametrize(
    ("descriptor_present", "extra_files"),
    [
        (True, ["launcher-stdout.log", "launcher-stderr.log", "stray.txt"]),
        (True, ["coverage.xml"]),
        (False, ["launcher-stdout.log", "launcher-stderr.log"]),
    ],
    ids=[
        "whitelisted-logs-plus-stray",
        "descriptor-plus-stale-coverage-xml",
        "logs-without-descriptor",
    ],
)
def test_full_coverage_staging_directory_rejection_matrix(
    tmp_path: Path,
    stub_uv_dir: Path,
    descriptor_present: bool,
    extra_files: list[str],
):
    """The remaining whitelist-boundary rejection rows from the spec example
    table (CHANGE task 2.2), distinct from the near-miss-by-name cases above:

    - Both whitelisted log files present does NOT launder an additional
      unknown entry (``stray.txt``) into acceptance -- the whitelist is
      closed, not merely "descriptor plus at most the two named logs are
      ignored".
    - Stale evidence (``coverage.xml``) beside a valid descriptor is refused
      exactly like any other unlisted entry, even though it is a plausible
      FullCoverage output name rather than an arbitrary stray file.
    - The two launcher log files alone, with NO run-descriptor.json, never
      satisfy the whitelist: the descriptor is required, not merely
      permitted, so log-only never passes regardless of which optional
      entries accompany it.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    if descriptor_present:
        _write_run_descriptor(staging, run_id="run-a")
    else:
        staging.mkdir(parents=True)
    for name in extra_files:
        (staging / name).write_text("content", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a staging directory outside the whitelist "
        f"(descriptor_present={descriptor_present}, extra_files={extra_files})"
    )
    assert "must contain only the whitelisted entries" in result.stderr, result.stderr


def test_full_coverage_descriptor_runid_mismatch_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    # The directory is named after the invocation's run ID, but the
    # descriptor inside it names a different run -- distinguished from the
    # dead-owner case below by asserting the identity-mismatch message, not
    # merely the shared exit code.
    _write_run_descriptor(staging, run_id="run-b")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a run-ID identity mismatch"
    assert "identity does not match the invocation" in result.stderr


def test_full_coverage_descriptor_dead_owner_process_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a", owner_process_id=_dead_process_id())
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a dead owner process"
    assert "names a dead owner process" in result.stderr


def test_full_coverage_descriptor_owner_alive_but_unrelated_is_refused(
    tmp_path: Path, stub_uv_dir: Path
):
    """A live PID alone must not satisfy ownership.

    The descriptor's ownerProcessId must identify the actual OS parent of
    this invocation (the process that spawned the launcher), not merely any
    live, unrelated process. Otherwise a run ID's descriptor could be reused
    or forged while the recorded owner PID happens to still be alive under an
    unrelated process, letting an unauthorized caller pass this check.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"

    # A live process that is NOT this test's own PID (the actual OS parent of
    # the launcher process spawned below via subprocess.run) and NOT the
    # launcher process itself -- a genuinely unrelated, merely-alive PID.
    unrelated_proc = subprocess.Popen(
        ["pwsh", "-NoProfile", "-Command", "Start-Sleep -Seconds 30"]
    )
    try:
        _write_run_descriptor(
            staging, run_id="run-a", owner_process_id=unrelated_proc.pid
        )
        marker = tmp_path / "marker.txt"
        env = _base_env(stub_uv_dir, marker)

        result = _run_launcher(
            [
                "-Route",
                "FullCoverage",
                "-CoverageRunId",
                "run-a",
                "-RuntimeRoot",
                str(runtime_root),
            ],
            env=env,
        )

        assert result.returncode == 19, (result.stdout, result.stderr)
        assert not marker.exists(), (
            "uv was invoked despite an owner PID unrelated to this invocation"
        )
        assert (
            "does not match this invocation's actual parent process" in result.stderr
        ), result.stderr
    finally:
        unrelated_proc.kill()
        unrelated_proc.wait(timeout=10)


def test_full_coverage_run_id_resolving_into_another_runs_directory_is_refused(
    tmp_path: Path, stub_uv_dir: Path
):
    """A forged sibling: -CoverageRunId names a directory that is really a
    junction onto a different run's staging directory. This must be refused
    even though Test-Path sees a container and a descriptor is readable
    through it -- the containment re-check after resolving the real path is
    what catches it, distinct from every run-descriptor-shape check above
    (hence the different exit code, 18, not 19).
    """
    runtime_root = tmp_path / "runtime"
    real_staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(real_staging, run_id="run-a")

    forged_staging = runtime_root / "coverage" / "run-b"
    _create_directory_junction(forged_staging, real_staging)

    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-b",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 18, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a forged sibling staging directory"
    )
    assert "escapes the artifact root" in result.stderr


def test_full_coverage_pytest_vector_equals_full_plus_exactly_five_coverage_options(
    tmp_path: Path, stub_uv_dir: Path
):
    """Argument-assembly only -- this does not exercise a full pytest run.

    The first three elements of the coverage tail are literal and independent
    of path resolution, so they are compared by exact string equality, same
    as every other route-vector test in this file. The XML/junit report
    paths are built from the launcher's own Win32-resolved real path (which
    may carry an extended-length ``\\\\?\\`` prefix), so those two elements
    are compared via ``Path.resolve()`` rather than exact string equality.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "FullCoverage",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    args = parsed["args"]
    assert isinstance(args, list)

    full_vector = ["-n", "3", "--dist=load", "-q", "tests"]
    expected_prefix = ["run", "--locked", "--exact", "pytest", *full_vector]
    assert args[: len(expected_prefix)] == expected_prefix, args

    tail = args[len(expected_prefix) :]
    assert len(tail) == 6, tail
    assert tail[0] == "--cov=src"
    assert tail[1] == "--cov-branch"
    assert tail[2] == "--cov-report=term-missing"

    def _resolved_path_from_flag(flag_value: str, flag_prefix: str) -> Path:
        # The launcher builds these paths from its own Win32-resolved real
        # path, which may carry an extended-length "\\?\" prefix that plain
        # Path.resolve() does not strip back off -- normalize both sides the
        # same way before comparing.
        path_text = flag_value[len(flag_prefix) :]
        if path_text.startswith("\\\\?\\"):
            path_text = path_text[len("\\\\?\\") :]
        return Path(path_text).resolve()

    xml_flag_prefix = "--cov-report=xml:"
    assert tail[3].startswith(xml_flag_prefix), tail[3]
    assert (
        _resolved_path_from_flag(tail[3], xml_flag_prefix)
        == (staging / "coverage.xml").resolve()
    )

    junit_flag_prefix = "--junitxml="
    assert tail[4].startswith(junit_flag_prefix), tail[4]
    assert (
        _resolved_path_from_flag(tail[4], junit_flag_prefix)
        == (staging / "junit.xml").resolve()
    )

    assert tail[5] == f"--basetemp={runtime_root / 'pytest'}"


# ---------------------------------------------------------------------------
# Task 5.1/5.2 (#75 round 4) -- Full route with -CoverageRunId, the second
# enumerated grant of the parameter (design.md > "Bind collection evidence to
# the run that produced it"). Full runs second, into a directory FullCoverage
# already populated, so it verifies the volume/reparse decision, path
# containment, and the run descriptor's identity, but does NOT apply the
# enumerated whitelist FullCoverage enforces as the first evidence writer
# (run-descriptor.json plus, optionally, launcher-stdout.log and/or
# launcher-stderr.log -- see the "FullCoverage route -- argument contract and
# staging-directory fail-closed checks" section above).
# ---------------------------------------------------------------------------


def _resolved_path_from_junit_flag(flag_value: str) -> Path:
    """Same Win32-real-path normalization as the FullCoverage vector test
    above: the launcher builds these paths from its own resolved real path,
    which may carry an extended-length ``\\\\?\\`` prefix that plain
    ``Path.resolve()`` does not strip back off.
    """
    prefix = "--junitxml="
    path_text = flag_value[len(prefix) :]
    if path_text.startswith("\\\\?\\"):
        path_text = path_text[len("\\\\?\\") :]
    return Path(path_text).resolve()


def test_full_with_run_id_writes_junit_into_its_own_staging_directory_only(
    tmp_path: Path, stub_uv_dir: Path
):
    """A Full run with a run ID writes its JUnit report inside that run's own
    staging directory -- never the shared "pytest" leaf, and nowhere else.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    # Simulates the directory FullCoverage already populated: in the real
    # orchestration (report-diff-coverage.ps1), FullCoverage always runs
    # first and leaves the run descriptor plus its own outputs behind before
    # Full ever runs with the same run ID.
    _write_run_descriptor(staging, run_id="run-a")
    (staging / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
    (staging / "junit.xml").write_text("<testsuites/>", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    args = parsed["args"]
    assert isinstance(args, list)
    junit_args = [a for a in args if a.startswith("--junitxml=")]
    assert len(junit_args) == 1, args
    assert (
        _resolved_path_from_junit_flag(junit_args[0])
        == (staging / "full-junit.xml").resolve()
    )

    # "Nowhere else": no other junit report is written at all -- a bare
    # Full invocation writes none (see
    # test_full_without_run_id_writes_no_junit_report below), so there is no
    # separate shared-name artifact left to check for.
    assert len([a for a in args if a.startswith("--junitxml=")]) == 1, args
    pytest_leaf = runtime_root / "pytest"
    assert not pytest_leaf.exists() or not list(pytest_leaf.glob("*.xml")), (
        "Full with a run ID must not also write a junit report into the "
        "shared 'pytest' leaf"
    )


def test_full_with_run_id_permits_a_populated_directory_full_coverage_refuses(
    tmp_path: Path, stub_uv_dir: Path
):
    """Same staging-directory shape as
    test_full_coverage_staging_directory_with_extra_file_exits_19 (which
    asserts FullCoverage refuses it because, as the first evidence writer, it
    accepts only its enumerated whitelist -- run-descriptor.json plus,
    optionally, launcher-stdout.log and/or launcher-stderr.log -- and
    ``stray.txt`` falls outside it) -- here Full must accept it, because it
    legitimately runs second into a directory FullCoverage already filled and
    does not apply that whitelist.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    (staging / "stray.txt").write_text("unexpected", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists(), (
        "Full with a run ID wrongly refused an already-populated staging directory"
    )


def test_full_with_run_id_pre_existing_junit_file_is_refused_before_pytest(
    tmp_path: Path, stub_uv_dir: Path
):
    """The provenance guarantee this round exists for: if a prior, unrelated
    run (or a dead pytest that never finished writing) already left a
    full-junit.xml at this exact destination, a fresh Full invocation must
    refuse rather than silently let pytest overwrite -- or, if THIS pytest
    dies first, let a stale file be read afterward as this run's evidence.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    (staging / "full-junit.xml").write_text(
        "<testsuites><testsuite/></testsuites>", encoding="utf-8"
    )
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a pre-existing full-junit.xml destination"
    )
    assert "already exists" in result.stderr, result.stderr
    assert "full-junit.xml" in result.stderr, result.stderr


def test_full_with_run_id_pre_existing_junit_directory_is_refused_before_pytest(
    tmp_path: Path, stub_uv_dir: Path
):
    """Same guarantee as the file case above, but the pre-existing entry is a
    directory rather than a file -- a check that merely tried to read the
    destination as a junit report (or used -PathType Leaf) would miss this
    shape.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    (staging / "full-junit.xml").mkdir()
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a pre-existing full-junit.xml directory"
    )
    assert "already exists" in result.stderr, result.stderr
    assert "full-junit.xml" in result.stderr, result.stderr


def test_full_with_run_id_pre_existing_junit_reparse_point_is_refused_before_pytest(
    tmp_path: Path, stub_uv_dir: Path
):
    """The pre-existing entry can also be a reparse point -- e.g. a junction
    planted at the destination name pointing somewhere else entirely, with a
    target that still resolves. The guard must react to the entry's own
    presence, not to whether its target resolves -- see
    test_full_with_run_id_dangling_junit_reparse_point_is_refused_before_pytest
    below for the complementary dangling-target case, which this
    still-resolvable-target case does NOT exercise.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _create_directory_junction(staging / "full-junit.xml", elsewhere)
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a pre-existing full-junit.xml reparse point"
    )
    assert "already exists" in result.stderr, result.stderr
    assert "full-junit.xml" in result.stderr, result.stderr


def test_full_with_run_id_dangling_junit_reparse_point_is_refused_before_pytest(
    tmp_path: Path, stub_uv_dir: Path
):
    """The pre-existing entry can be a DANGLING reparse point -- a junction
    planted at the destination name whose target has since been removed.

    This is the case the sibling test above (still-resolvable target) does
    NOT exercise: a Test-Path/File.Exists-style check follows a reparse
    point to its target, so a dangling one resolves to "does not exist" and
    such a check would wrongly let pytest proceed to write here. The guard
    must react to the entry's own presence via attributes that do not
    require following the link, so a regression to a target-following check
    would still pass every other case in this file but fail this one.
    """
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    _write_run_descriptor(staging, run_id="run-a")
    vanishing_target = tmp_path / "vanishing-target"
    vanishing_target.mkdir()
    _create_directory_junction(staging / "full-junit.xml", vanishing_target)
    vanishing_target.rmdir()
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), (
        "uv was invoked despite a pre-existing dangling full-junit.xml reparse point"
    )
    assert "already exists" in result.stderr, result.stderr
    assert "full-junit.xml" in result.stderr, result.stderr


def test_full_with_run_id_descriptor_mismatch_is_refused_before_pytest(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    staging = runtime_root / "coverage" / "run-a"
    # The directory is named after the invocation's run ID, but the
    # descriptor inside it names a different run.
    _write_run_descriptor(staging, run_id="run-b")
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a run-ID identity mismatch"
    assert "identity does not match the invocation" in result.stderr


def test_full_with_run_id_missing_staging_directory_exits_19(
    tmp_path: Path, stub_uv_dir: Path
):
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "run-a",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 19, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite a missing staging directory"
    assert "does not exist" in result.stderr


def test_full_with_invalid_run_id_spelling_is_rejected_before_launch(
    tmp_path: Path, stub_uv_dir: Path
):
    """The same run-ID shape guard applies to Full as to FullCoverage -- it
    is not a FullCoverage-only check.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        [
            "-Route",
            "Full",
            "-CoverageRunId",
            "a/b",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        env=env,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not marker.exists(), "uv was invoked despite an invalid run ID"
    assert "is invalid" in result.stderr


def test_full_without_run_id_writes_no_junit_report(tmp_path: Path, stub_uv_dir: Path):
    """Acceptance (#75 round 4): a plain Full invocation supplying no
    -CoverageRunId carries NO --junitxml element at all -- deliberately
    identical to version 1 (before #75), not to version 2. That report has
    exactly one reader (scripts/report-diff-coverage.ps1's comparison read),
    and that reader always supplies a run ID now, so an unbound Full
    invocation has no reader to produce a report for. Contrast with
    test_full_with_run_id_writes_junit_into_its_own_staging_directory_only
    above, which is the -CoverageRunId half of the same contract.
    """
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "marker.txt"
    env = _base_env(stub_uv_dir, marker)

    result = _run_launcher(
        ["-Route", "Full", "-RuntimeRoot", str(runtime_root)],
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = _parse_marker(marker)
    expected_full_args = [
        "run",
        "--locked",
        "--exact",
        "pytest",
        "-n",
        "3",
        "--dist=load",
        "-q",
        "tests",
        f"--basetemp={runtime_root / 'pytest'}",
    ]
    assert parsed["args"] == expected_full_args
    args = parsed["args"]
    assert isinstance(args, list)
    assert not [a for a in args if a.startswith("--junitxml=")], args
