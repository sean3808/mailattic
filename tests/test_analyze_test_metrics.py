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
    assert audit["schema_version"] == 2
    file_candidates_by_member = {
        member: c for c in audit["test_file_candidates"] for member in c["members"]
    }
    assert "tests/test_subset.py" in file_candidates_by_member
    assert "tests/test_contract.py" not in file_candidates_by_member
    assert "tests/test_contract.py" in audit["zero_src_coverage_test_files"]
    supersets = [
        member
        for s in file_candidates_by_member["tests/test_subset.py"][
            "superset_test_files"
        ]
        for member in s["members"]
    ]
    assert "tests/test_alpha.py" in supersets

    single_candidates_by_member = {
        member: c for c in audit["single_test_candidates"] for member in c["members"]
    }
    assert "tests/test_subset.py::test_small" in single_candidates_by_member
    small_group = single_candidates_by_member["tests/test_subset.py::test_small"]
    assert small_group["member_count"] == 2
    assert set(small_group["members"]) == {
        "tests/test_alpha.py::test_fast",
        "tests/test_subset.py::test_small",
    }
    single_supersets = [
        member for s in small_group["superset_tests"] for member in s["members"]
    ]
    assert "tests/test_alpha.py::AlphaTests::test_slow" in single_supersets

    # Every coverage context joined to a JUnit test (test_docs_only has JUnit
    # only, which is fine: it touches no src/ line).
    assert durations["coverage_nodeids_missing_from_junit"] == []
    assert durations["failed_tests"] == []

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


def test_analyze_fails_loudly_when_coverage_nodeids_do_not_join_junit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_alpha.py").write_text("", encoding="utf-8")
    src_file = repo / "src" / "mailattic" / "alpha.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    junit_xml = tmp_path / "junit.xml"
    _write_junit_xml(junit_xml, [("tests.test_alpha", "test_one", 0.1, "passed")])

    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        lines_by_context={
            # Same test, but the nodeid shape differs from what JUnit rebuilds.
            "tests\\sub\\test_alpha.py::test_one[case-1]|run": {str(src_file): [1]},
        },
    )

    out_dir = tmp_path / "reports"
    result = _run_analyzer(junit_xml, cov_db, out_dir, repo_root=repo)
    assert result.returncode != 0
    assert "no matching junit test" in (result.stdout + result.stderr).lower()


def test_parametrize_id_backslashes_survive_the_join(tmp_path: Path) -> None:
    # Real CI run 43811470631: ids like "[\\udce9]" were rewritten to "[/udce9]",
    # so 70 coverage contexts no longer matched their JUnit tests.
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_alpha.py").write_text("", encoding="utf-8")
    src_file = repo / "src" / "mailattic" / "alpha.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    param_name = "test_one[x\\ud800\\r\\n]"
    junit_xml = tmp_path / "junit.xml"
    _write_junit_xml(junit_xml, [("tests.test_alpha", param_name, 0.1, "passed")])
    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        lines_by_context={
            f"tests\\test_alpha.py::{param_name}|run": {str(src_file): [1]},
        },
    )

    result = _run_analyzer(junit_xml, cov_db, tmp_path / "reports", repo_root=repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    durations = json.loads(
        (tmp_path / "reports" / "durations.json").read_text(encoding="utf-8")
    )
    assert durations["coverage_nodeids_missing_from_junit"] == []
    mapping = json.loads(
        (tmp_path / "reports" / "source-to-tests.json").read_text(encoding="utf-8")
    )
    assert mapping["source_to_test_files"]["src/mailattic/alpha.py"] == [
        "tests/test_alpha.py"
    ]


def _failure_ratio_fixture(
    tmp_path: Path, *, total: int, failed: int
) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_alpha.py").write_text("", encoding="utf-8")
    src_file = repo / "src" / "mailattic" / "alpha.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    junit_xml = tmp_path / "junit.xml"
    _write_junit_xml(
        junit_xml,
        [
            (
                "tests.test_alpha",
                f"test_{i}",
                0.01,
                "failure" if i < failed else "passed",
            )
            for i in range(total)
        ],
    )
    cov_db = tmp_path / ".coverage"
    _build_coverage_db(
        cov_db,
        lines_by_context={"tests/test_alpha.py::test_0|run": {str(src_file): [1]}},
    )
    return _run_analyzer(junit_xml, cov_db, tmp_path / "reports", repo_root=repo)


def test_analyze_tolerates_a_few_failed_tests_and_lists_them(tmp_path: Path) -> None:
    # 1 of 100 is at the 1% limit: still reported, failures listed.
    result = _failure_ratio_fixture(tmp_path, total=100, failed=1)
    assert result.returncode == 0, (result.stdout, result.stderr)
    durations = json.loads(
        (tmp_path / "reports" / "durations.json").read_text(encoding="utf-8")
    )
    assert durations["failed_tests"] == ["tests/test_alpha.py::test_0"]


def test_analyze_fails_loudly_when_too_many_tests_failed(tmp_path: Path) -> None:
    result = _failure_ratio_fixture(tmp_path, total=100, failed=2)
    assert result.returncode != 0
    assert "too many for the coverage map" in (result.stdout + result.stderr)


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


def _run_audit_fixture(
    tmp_path: Path,
    name: str,
    test_cases: list[tuple[str, str, dict[str, list[int]]]],
) -> dict[str, object]:
    """Create a synthetic repo, run the analyzer, and return audit-candidates.json."""
    base = tmp_path / name
    repo = base / "repo"
    (repo / "src" / "mailattic").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    src_paths: dict[str, Path] = {}
    for _, _, file_lines in test_cases:
        for src_name in file_lines:
            if src_name not in src_paths:
                p = repo / "src" / "mailattic" / src_name
                p.write_text("x = 1\n", encoding="utf-8")
                src_paths[src_name] = p

    for mod, _, _ in test_cases:
        (repo / f"{mod.replace('.', '/')}.py").write_text("", encoding="utf-8")

    junit_xml = base / "junit.xml"
    _write_junit_xml(
        junit_xml,
        [(mod, tc_name, 0.01, "passed") for mod, tc_name, _ in test_cases],
    )
    lines_by_context = {
        f"{mod.replace('.', '/')}.py::{tc_name}|run": {
            str(src_paths[src_name]): lines for src_name, lines in file_lines.items()
        }
        for mod, tc_name, file_lines in test_cases
    }
    cov_db = base / ".coverage"
    _build_coverage_db(cov_db, lines_by_context=lines_by_context)

    out_dir = base / "reports"
    result = _run_analyzer(junit_xml, cov_db, out_dir, repo_root=repo)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads((out_dir / "audit-candidates.json").read_text(encoding="utf-8"))


def test_grouped_candidates_match_brute_force_reference(tmp_path: Path) -> None:
    test_cases = [
        # Identical single-test masks in the same file
        ("tests.test_ident", "test_eq_a", {"alpha.py": [1, 2]}),
        ("tests.test_ident", "test_eq_b", {"alpha.py": [1, 2]}),
        # Strict subset vs superset
        ("tests.test_sub", "test_sub", {"alpha.py": [1]}),
        ("tests.test_super", "test_super", {"alpha.py": [1, 2, 3]}),
        # Equal-but-distinct files: test_file_a has 1 test covering {10, 11};
        # test_file_b has 2 tests covering {10} and {11} respectively.
        ("tests.test_file_a", "test_a_all", {"beta.py": [10, 11]}),
        ("tests.test_file_b", "test_b_part1", {"beta.py": [10]}),
        ("tests.test_file_b", "test_b_part2", {"beta.py": [11]}),
        # Disjoint mask (not a subset of anything, no equal peers)
        ("tests.test_disjoint", "test_alone", {"beta.py": [99]}),
    ]
    audit = _run_audit_fixture(tmp_path, "brute_force", test_cases)
    assert audit["schema_version"] == 2

    # Independent brute-force reference derived directly from fixture line sets.
    node_sets: dict[str, set[tuple[str, int]]] = {}
    file_sets: dict[str, set[tuple[str, int]]] = {}
    for mod, name, file_lines in test_cases:
        tf = f"{mod.replace('.', '/')}.py"
        nid = f"{tf}::{name}"
        pts = {(path, line) for path, lines in file_lines.items() for line in lines}
        node_sets[nid] = pts
        file_sets.setdefault(tf, set()).update(pts)

    expected_single = {
        a
        for a, sa in node_sets.items()
        if sa and any(b != a and sa <= sb for b, sb in node_sets.items())
    }
    expected_files = {
        a
        for a, sa in file_sets.items()
        if sa and any(b != a and sa <= sb for b, sb in file_sets.items())
    }

    actual_single = {
        member
        for group in audit["single_test_candidates"]
        for member in group["members"]
    }
    actual_files = {
        member for group in audit["test_file_candidates"] for member in group["members"]
    }

    assert actual_single == expected_single
    assert actual_files == expected_files
    assert audit["single_test_candidate_count"] == len(expected_single)
    assert audit["test_file_candidate_count"] == len(expected_files)


def test_identical_tests_produce_linear_output_and_truncate_supersets(
    tmp_path: Path,
) -> None:
    audit_10 = _run_audit_fixture(
        tmp_path,
        "k10",
        [
            ("tests.test_param", f"test_case[{i}]", {"alpha.py": [1, 2, 3]})
            for i in range(10)
        ],
    )
    audit_50 = _run_audit_fixture(
        tmp_path,
        "k50",
        [
            ("tests.test_param", f"test_case[{i}]", {"alpha.py": [1, 2, 3]})
            for i in range(50)
        ],
    )

    assert len(audit_50["single_test_candidates"]) == 1
    group_50 = audit_50["single_test_candidates"][0]
    assert group_50["member_count"] == 50
    assert len(group_50["members"]) == 50
    assert group_50["superset_count"] == 0
    assert group_50["superset_tests"] == []

    size_10 = len(json.dumps(audit_10["single_test_candidates"]))
    size_50 = len(json.dumps(audit_50["single_test_candidates"]))
    # O(k) grows ~5x from k=10 to k=50; O(k^2) would grow ~25x.
    assert size_50 < 6 * size_10

    # Verify superset truncation + total superset_count when > max_superset_groups.
    superset_n = 15
    trunc_cases = [("tests.test_trunc", "test_base", {"alpha.py": [1]})] + [
        ("tests.test_trunc", f"test_sup_{i}", {"alpha.py": [1, 10 + i]})
        for i in range(superset_n)
    ]
    audit_trunc = _run_audit_fixture(tmp_path, "trunc", trunc_cases)
    base_group = audit_trunc["single_test_candidates"][0]
    assert base_group["members"] == ["tests/test_trunc.py::test_base"]
    assert base_group["superset_count"] == superset_n
    assert len(base_group["superset_tests"]) == audit_trunc["max_superset_groups"]
