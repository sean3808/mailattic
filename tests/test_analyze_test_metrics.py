"""Unit tests for scripts/analyze_test_metrics.py using synthetic fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import coverage
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "analyze_test_metrics.py"


def _write_junit_xml(path: Path, cases: list[tuple[str, str, float, str]]) -> None:
    """Write a minimal pytest-style JUnit XML file."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<testsuites>",
        '  <testsuite name="pytest">',
    ]
    for classname, name, duration, outcome in cases:
        lines.append(
            f'    <testcase classname="{classname}" '
            f'name="{name}" time="{duration:.6f}">'
        )
        if outcome == "failure":
            lines.append('      <failure message="boom">trace</failure>')
        elif outcome == "skipped":
            lines.append('      <skipped message="skip"/>')
        lines.append("    </testcase>")
    lines.extend(["  </testsuite>", "</testsuites>"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_coverage_db(
    db_path: Path,
    *,
    arcs_by_context: dict[str, dict[str, list[tuple[int, int]]]] | None = None,
    lines_by_context: dict[str, dict[str, list[int]]] | None = None,
) -> None:
    """Create a real coverage.py SQLite DB with per-test contexts in tmp_path."""
    cov_data = coverage.CoverageData(basename=str(db_path))
    if arcs_by_context is not None:
        for ctx, file_arcs in arcs_by_context.items():
            cov_data.set_context(ctx)
            cov_data.add_arcs(file_arcs)
    if lines_by_context is not None:
        for ctx, file_lines in lines_by_context.items():
            cov_data.set_context(ctx)
            cov_data.add_lines(file_lines)
    cov_data.write()


def _run_analyzer(
    junit_xml: Path,
    coverage_db: Path,
    output_dir: Path,
    *,
    repo_root: Path,
    top_n: int = 10,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--junit-xml",
            str(junit_xml),
            "--coverage-db",
            str(coverage_db),
            "--output-dir",
            str(output_dir),
            "--repo-root",
            str(repo_root),
            "--top-n",
            str(top_n),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_analyze_produces_all_three_reports_and_summary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    alpha_src = repo / "src" / "mailattic" / "alpha.py"
    beta_src = repo / "src" / "mailattic" / "beta.py"
    alpha_src.write_text("x = 1\n", encoding="utf-8")
    beta_src.write_text("y = 2\n", encoding="utf-8")
    (repo / "tests" / "test_alpha.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_beta.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_subset.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_contract.py").write_text("", encoding="utf-8")

    junit_xml = tmp_path / "junit.xml"
    _write_junit_xml(
        junit_xml,
        [
            ("tests.test_alpha", "test_fast", 0.5, "passed"),
            ("tests.test_alpha.AlphaTests", "test_slow", 4.0, "passed"),
            ("tests.test_beta", "test_beta_main", 2.0, "passed"),
            ("tests.test_subset", "test_small", 1.0, "passed"),
            ("tests.test_contract", "test_docs_only", 0.2, "passed"),
        ],
    )

    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        arcs_by_context={
            # Import-time context must be ignored in source-to-tests attribution.
            "": {str(alpha_src): [(1, 2)], str(beta_src): [(1, 2)]},
            "tests/test_alpha.py::test_fast|run": {
                str(alpha_src): [(-1, 10), (10, 11)],
            },
            "tests/test_alpha.py::AlphaTests::test_slow|run": {
                str(alpha_src): [(10, 11), (11, 12), (12, 13)],
                str(beta_src): [(20, 21)],
            },
            "tests/test_beta.py::test_beta_main|run": {
                str(beta_src): [(20, 21), (21, 22)],
            },
            # Subset of tests/test_alpha.py (covers only alpha lines 10, 11).
            "tests/test_subset.py::test_small|run": {
                str(alpha_src): [(10, 11)],
            },
        },
    )

    out_dir = tmp_path / "reports"
    result = _run_analyzer(junit_xml, cov_db, out_dir, repo_root=repo, top_n=3)
    assert result.returncode == 0, (result.stdout, result.stderr)

    durations = json.loads((out_dir / "durations.json").read_text(encoding="utf-8"))
    assert durations["total_tests"] == 5
    assert durations["total_duration_seconds"] == pytest.approx(7.7)
    assert len(durations["top_tests"]) == 3
    assert (
        durations["top_tests"][0]["nodeid"]
        == "tests/test_alpha.py::AlphaTests::test_slow"
    )
    assert durations["top_tests"][0]["duration_seconds"] == pytest.approx(4.0)
    # Every test's duration is in the artifact, not only the top-N (#79).
    assert len(durations["all_tests"]) == 5
    assert durations["all_tests"][:3] == durations["top_tests"]

    assert durations["top_test_files"][0]["test_file"] == "tests/test_alpha.py"
    assert durations["top_test_files"][0]["duration_seconds"] == pytest.approx(4.5)
    assert durations["top_test_files"][0]["test_count"] == 2

    top_modules = {m["source_file"]: m for m in durations["top_source_modules"]}
    # alpha.py is touched by test_fast (0.5), test_slow (4.0), test_small (1.0) => 5.5s
    assert top_modules["src/mailattic/alpha.py"][
        "total_touching_duration_seconds"
    ] == pytest.approx(5.5)
    # test_slow touches 2 modules (4.0/2 = 2.0) + 0.5 + 1.0 = 3.5s proportional
    assert top_modules["src/mailattic/alpha.py"][
        "proportional_duration_seconds"
    ] == pytest.approx(3.5)

    mapping = json.loads((out_dir / "source-to-tests.json").read_text(encoding="utf-8"))
    # Both files ran lines under the empty (import-time) context above; the map
    # cannot attribute those to a test, so the report names them.
    assert mapping["source_files_with_uncontexted_lines"] == [
        "src/mailattic/alpha.py",
        "src/mailattic/beta.py",
    ]
    assert mapping["source_to_test_files"]["src/mailattic/alpha.py"] == [
        "tests/test_alpha.py",
        "tests/test_subset.py",
    ]
    assert mapping["source_to_test_files"]["src/mailattic/beta.py"] == [
        "tests/test_alpha.py",
        "tests/test_beta.py",
    ]

    audit = json.loads((out_dir / "audit-candidates.json").read_text(encoding="utf-8"))
    file_candidates = {c["test_file"]: c for c in audit["test_file_candidates"]}
    assert "tests/test_subset.py" in file_candidates
    assert "tests/test_contract.py" not in file_candidates
    assert "tests/test_contract.py" in audit["zero_src_coverage_test_files"]
    supersets = [
        s["test_file"]
        for s in file_candidates["tests/test_subset.py"]["superset_test_files"]
    ]
    assert "tests/test_alpha.py" in supersets

    single_candidates = {c["nodeid"]: c for c in audit["single_test_candidates"]}
    assert "tests/test_subset.py::test_small" in single_candidates
    single_supersets = [
        s["nodeid"]
        for s in single_candidates["tests/test_subset.py::test_small"]["superset_tests"]
    ]
    assert "tests/test_alpha.py::test_fast" in single_supersets
    assert "tests/test_alpha.py::AlphaTests::test_slow" in single_supersets

    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "Top Slowest Tests" in summary
    assert "Source-to-Test Mapping" in summary
    assert "Audit Candidates" in summary


def test_analyze_fails_loudly_when_coverage_db_has_no_test_contexts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    src_file = repo / "src" / "mailattic" / "alpha.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    junit_xml = tmp_path / "junit.xml"
    _write_junit_xml(junit_xml, [("tests.test_alpha", "test_one", 0.1, "passed")])

    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        lines_by_context={"": {str(src_file): [1, 2]}},
    )

    out_dir = tmp_path / "reports"
    result = _run_analyzer(junit_xml, cov_db, out_dir, repo_root=repo)
    assert result.returncode != 0
    assert "context" in (result.stdout + result.stderr).lower()


def test_analyze_fails_loudly_on_missing_or_empty_junit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    src_file = repo / "src" / "mailattic" / "alpha.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    junit_xml = tmp_path / "junit.xml"
    junit_xml.write_text("<testsuites></testsuites>", encoding="utf-8")

    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        lines_by_context={"tests/test_alpha.py::test_one|run": {str(src_file): [1]}},
    )

    out_dir = tmp_path / "reports"
    result = _run_analyzer(junit_xml, cov_db, out_dir, repo_root=repo)
    assert result.returncode != 0
    assert "testcase" in (result.stdout + result.stderr).lower()
