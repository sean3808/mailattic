"""Regression contracts for the layered pytest and release gates."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

from tests._pwsh_support import ps_quote as _ps_quote
from tests._pwsh_support import run_pwsh_file

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_CONFIG = REPO_ROOT / "uv.toml"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.ps1"
LAUNCHER_SCRIPT = REPO_ROOT / "scripts" / "invoke-pytest.ps1"
PROJECT_TOOL_ENVIRONMENT_SCRIPT = REPO_ROOT / "scripts" / "project-tool-environment.ps1"
QUALITY_GATES = REPO_ROOT / "docs" / "agents" / "quality-gates.md"
EXTERNAL_SKILL_ROUTING = REPO_ROOT / "docs" / "agents" / "external-skill-routing.md"
MARKER_LEDGER = REPO_ROOT / "docs" / "agents" / "pytest-marker-ledger.md"
GITIGNORE = REPO_ROOT / ".gitignore"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

RAW_PYTEST_COMMAND = "uv run --locked --exact pytest"

# FullCoverage's own instrumentation flag. It is a route of the same
# launcher, not a second entry point, so its literal presence outside the
# launcher is governed by the identical rule: some other script or doc would
# be invoking pytest with coverage directly, bypassing the one route this
# change establishes.
RAW_COVERAGE_INSTRUMENTATION_FLAG = "--cov=src"

# The design's own contract phrasing: raw canonical pytest command strings
# may exist only inside the launcher itself or inside purpose-built tests
# (tests/ is where the exact string is asserted against, never invoked).
RAW_PYTEST_COMMAND_ALLOWED_PATHS = (LAUNCHER_SCRIPT,)

# Every marker whose literal presence outside the launcher means some other
# path is invoking pytest (with or without coverage instrumentation)
# directly instead of going through scripts/invoke-pytest.ps1.
RAW_PYTEST_INVOCATION_MARKERS = (RAW_PYTEST_COMMAND, RAW_COVERAGE_INSTRUMENTATION_FLAG)

LAUNCHER_ROUTE_BY_SECTION = {
    "targeted": "Targeted",
    "quick": "Quick",
    "full": "Full",
    "full-sequential": "FullSequential",
}


class _GitHubActionsLoader(yaml.SafeLoader):
    """Parse YAML booleans without treating the GitHub Actions `on` key as true."""


_GitHubActionsLoader.yaml_implicit_resolvers = {
    key: [resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_GitHubActionsLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _ci_workflow() -> dict[str, object]:
    return yaml.load(
        CI_WORKFLOW.read_text(encoding="utf-8"), Loader=_GitHubActionsLoader
    )


def test_hosted_windows_full_is_frequency_bounded_and_canonical() -> None:
    config = _ci_workflow()
    events = config["on"]
    jobs = config["jobs"]
    hosted_full = jobs["hosted-full"]

    assert events["schedule"] == [{"cron": "23 18 * * 0"}]
    assert "workflow_dispatch" in events
    assert hosted_full["if"] == (
        "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    )
    assert hosted_full["runs-on"] == "windows-latest"

    run_steps = [step["run"] for step in hosted_full["steps"] if "run" in step]
    assert "uv sync --locked" in run_steps
    assert "pwsh -NoProfile -File scripts/invoke-pytest.ps1 -Route Full" in run_steps

    hosted_contract = repr(hosted_full)
    assert "-RuntimeRoot" not in hosted_contract
    assert "MAILATTIC_ALLOW_SYSTEM_DRIVE_RUNTIME_ROOT" not in hosted_contract
    assert RAW_PYTEST_COMMAND not in hosted_contract

    expected_frequent_job_condition = (
        "github.event_name != 'schedule' && "
        "(github.event_name != 'pull_request' || "
        "github.event.pull_request.draft == false)"
    )
    assert jobs["static"]["if"] == expected_frequent_job_condition
    assert jobs["package"]["if"] == expected_frequent_job_condition
    assert config["concurrency"]["group"] == (
        "ci-${{ github.event_name }}-${{ github.ref }}"
    )


def test_ci_docs_distinguish_hosted_full_from_local_gate_evidence() -> None:
    # The cron itself is pinned by the workflow-contract test above; this one
    # owns only the documentation half of the contract.
    docs = _normalize_whitespace(QUALITY_GATES.read_text(encoding="utf-8"))

    assert "`hosted-full`" in docs
    assert "weekly schedule" in docs
    assert "manual dispatch" in docs
    assert "supplemental environment-compatibility probe" in docs
    assert "four or five automatic Full runs per month" in docs
    assert "does not replace local Full evidence for merge readiness or release" in docs
    assert "operational failure" in docs
    assert "No test is executed." not in docs


def _pytest_config() -> dict[str, str | list[str]]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]


def _strip_powershell_comments(text: str) -> str:
    """Drop `<# ... #>` block comments and whole-line `#` comments.

    Used before scanning scripts for forbidden invocation patterns so that
    documentation prose describing what a script deliberately does *not* do
    (e.g. a docstring that says "never calls setx") is not mistaken for the
    invocation itself.
    """
    without_blocks = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    kept_lines = [
        line for line in without_blocks.splitlines() if not line.strip().startswith("#")
    ]
    return "\n".join(kept_lines)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _tracked_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / line for line in output.splitlines() if line]


def test_pytest_discovery_and_marker_policy_is_explicit() -> None:
    config = _pytest_config()

    assert config["testpaths"] == ["tests"]
    assert set(config["markers"]) == {
        "slow: pathological MCP response-budget tests",
        "release: release, installation, or Git-hook end-to-end tests",
    }
    assert "--strict-markers" in config["addopts"]
    assert "-m " not in config["addopts"]


def test_pytest_temp_policy_is_repo_relative_and_ignored() -> None:
    config = _pytest_config()

    assert "--basetemp=.project-runtime/pytest" in config["addopts"]
    assert ".project-runtime/" in GITIGNORE.read_text(encoding="utf-8")
    temp_parent = Path(".project-runtime")
    python_temp = temp_parent / "python"
    pytest_basetemp = temp_parent / "pytest"
    ledger_basetemp = temp_parent / "ledger-collection"
    assert len({python_temp, pytest_basetemp, ledger_basetemp}) == 3
    assert all(
        path.parent == temp_parent
        for path in (python_temp, pytest_basetemp, ledger_basetemp)
    )


def test_uv_project_configuration_controls_cache_and_link_mode() -> None:
    config = tomllib.loads(UV_CONFIG.read_text(encoding="utf-8"))

    assert config["cache-dir"] == ".project-runtime/uv-cache"
    assert config["link-mode"] == "copy"
    assert "[tool.uv]" not in PYPROJECT.read_text(encoding="utf-8")


def test_launcher_environment_governs_the_uv_cache(tmp_path: Path) -> None:
    """The cache location must be observed, not constructed.

    uv gives `UV_CACHE_DIR` precedence over `uv.toml`, and this workstation
    sets that variable at user level. Deleting it from a copied environment
    before asking uv would test a machine that does not exist here, and would
    stay green even if the launcher stopped setting the variable at all. So
    the ambient environment is passed through untouched and the answer is
    taken from a child of the shared helper: it is right only because the
    helper overrode the user-level value.
    """
    runtime_root = tmp_path / "runtime"
    driver = tmp_path / "uv-cache-probe.ps1"
    driver.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". {_ps_quote(PROJECT_TOOL_ENVIRONMENT_SCRIPT)}\n"
        "$result = Invoke-InProjectToolEnvironment "
        f"-RepositoryRoot {_ps_quote(REPO_ROOT)} "
        f"-RuntimeRoot {_ps_quote(runtime_root)} "
        "-ChildFilePath 'uv' -ChildArgumentList @('cache', 'dir')\n"
        'Write-Output "CHILD_EXIT=$($result.ExitCode)"\n',
        encoding="utf-8",
    )

    result = run_pwsh_file(driver, env=os.environ.copy(), timeout=120)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "CHILD_EXIT=0" in result.stdout
    reported = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("CHILD_EXIT=")
    ]
    assert reported, (result.stdout, result.stderr)
    observed = Path(reported[-1]).resolve()
    assert observed == (runtime_root / "uv-cache").resolve()


def test_marked_node_ledger_is_explicit_and_complete() -> None:
    ledger = MARKER_LEDGER.read_text(encoding="utf-8")
    assert "| Node ID | Marker | Duration (s) | Rationale |" in ledger
    temp_parent = Path.cwd() / ".project-runtime"
    python_temp = temp_parent / "python"
    pytest_basetemp = temp_parent / "pytest"
    nested_basetemp = temp_parent / "ledger-collection"
    assert nested_basetemp not in {python_temp, pytest_basetemp}
    marked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "slow or release",
            "--basetemp",
            str(nested_basetemp),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    ledger_nodes = {
        line.split("|", 2)[1].strip()
        for line in ledger.splitlines()
        if line.startswith("|") and "tests/" in line
    }
    collected_nodes = {
        line.strip()
        for line in marked.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    assert collected_nodes == ledger_nodes


def test_quality_gate_document_publishes_four_routes() -> None:
    document = QUALITY_GATES.read_text(encoding="utf-8")
    for section_name, route_param in LAUNCHER_ROUTE_BY_SECTION.items():
        assert re.search(rf"^### {re.escape(section_name)}\b", document, re.MULTILINE)
        section = document.split(f"### {section_name}", 1)[1]
        section = section.split("### ", 1)[0]
        expected_command = f"scripts\\invoke-pytest.ps1 -Route {route_param}"
        assert expected_command in section, (section_name, section)

    targeted_section = document.split("### targeted", 1)[1].split("### ", 1)[0]
    assert "-RuntimeRoot ''" not in targeted_section
    assert "tests\\test_a.py::test_one tests\\test_b.py" in targeted_section
    assert "-Target tests\\test_a.py::test_one tests\\test_b.py" in targeted_section

    normalized_document = _normalize_whitespace(document)
    assert "not slow and not release" in normalized_document
    assert "is not release evidence" in normalized_document.lower()
    assert "current-task regression" in normalized_document
    assert "ordinary implementation feedback" in normalized_document
    assert "full for medium/high closeout" in normalized_document
    assert "full-sequential for fallback and equivalence proof" in normalized_document
    assert RAW_PYTEST_COMMAND not in document


def test_external_skill_routing_scopes_spectra_changes_to_current_worktree() -> None:
    document = EXTERNAL_SKILL_ROUTING.read_text(encoding="utf-8")
    normalized_document = _normalize_whitespace(document)

    required_contract = (
        "Before every mutation targeting an existing active change, use "
        "`git worktree list --porcelain` to enumerate every registered worktree "
        "root and test each `<worktree-root>/openspec/changes/<change>` path, "
        "including the current root from `git rev-parse --show-toplevel`. "
        "Exactly one match is required. If that match is the current root, run "
        "the mutation there; if it is another root, switch there and rerun the "
        "command. Zero matches (parked or misspelled change) or multiple matches "
        "are ambiguous: stop without running the mutation."
    )
    assert required_contract in document
    assert (
        "This owner gate does not apply to commands whose purpose is to create or "
        "restore the active directory, such as `spectra new` or `spectra unpark`."
    ) in document
    assert (
        "Spectra 2.3.1 discovers active change directories and task counts from "
        "registered sibling worktrees through the common Git directory."
    ) in normalized_document
    assert (
        "`spectra list --json` returned that sibling change with its exact task count."
    ) in normalized_document
    assert (
        "Running `spectra archive <change> --yes` from the calling tree then "
        "returned exit 0 while it deleted the active change, applied the delta to "
        "canonical specs, and created the archive **in the sibling worktree**."
    ) in normalized_document


def test_release_script_invokes_the_documented_full_route() -> None:
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert "scripts\\invoke-pytest.ps1" in script
    assert "-Route Full" in script
    assert "full pytest gate" in script.lower()
    release_tests_body = script[script.index("function Invoke-ReleaseTests") :]
    assert RAW_PYTEST_COMMAND not in release_tests_body


def test_documented_full_route_matches_release_invocation() -> None:
    """The doc and release must invoke the exact same launcher/route form --
    a divergence here (e.g. release quietly switching routes, or referencing
    a different launcher path) must fail this test, not just look consistent
    by eye.
    """
    document = QUALITY_GATES.read_text(encoding="utf-8")
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    release_tests_body = script[script.index("function Invoke-ReleaseTests") :]

    launcher_reference = "scripts\\invoke-pytest.ps1"
    full_route_flag = "-Route Full"

    assert launcher_reference in document
    assert launcher_reference in release_tests_body
    assert full_route_flag in document
    assert full_route_flag in release_tests_body


def test_project_runtime_defaults_avoid_the_system_drive() -> None:
    uv_config = tomllib.loads(UV_CONFIG.read_text(encoding="utf-8"))
    cache_dir = str(uv_config["cache-dir"])
    assert re.match(r"^[A-Za-z]:", cache_dir) is None, (
        f"uv.toml cache-dir must not be an absolute system-drive path: {cache_dir}"
    )
    assert cache_dir.replace("\\", "/").startswith(".project-runtime/")

    helper_script = PROJECT_TOOL_ENVIRONMENT_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"['\"]C:[\\/]", helper_script) is None, (
        "project-tool-environment.ps1 must not hardcode a C:-backed default "
        "runtime root"
    )


def test_repository_scripts_never_mutate_user_or_machine_environment() -> None:
    forbidden_patterns = (
        re.compile(
            r"\[Environment\]::SetEnvironmentVariable\([^)]*['\"](User|Machine)['\"]",
            re.IGNORECASE,
        ),
        re.compile(r"(?<![A-Za-z0-9_])setx(?![A-Za-z0-9_])", re.IGNORECASE),
    )
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "scripts").glob("*.ps1")):
        text = _strip_powershell_comments(path.read_text(encoding="utf-8"))
        for pattern in forbidden_patterns:
            if pattern.search(text):
                violations.append(str(path.relative_to(REPO_ROOT)))
    assert violations == [], (
        f"scripts/*.ps1 must never write User- or Machine-scoped environment "
        f"state: {violations}"
    )


def test_no_active_raw_pytest_command_outside_launcher_or_tests() -> None:
    violations: list[str] = []
    for path in _tracked_files():
        if not path.is_file() or path in RAW_PYTEST_COMMAND_ALLOWED_PATHS:
            continue
        try:
            relative = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            continue
        if relative.startswith("tests/") or relative.startswith("openspec/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in RAW_PYTEST_INVOCATION_MARKERS:
            if marker in text:
                violations.append(f"{relative} ({marker})")
    assert violations == [], (
        "raw pytest invocation (command or coverage flag) found outside the "
        f"launcher/tests: {violations}"
    )
