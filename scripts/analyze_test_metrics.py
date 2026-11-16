"""Analyze pytest JUnit XML durations and coverage.py per-test contexts.

Reads:
  - JUnit XML produced by pytest (``--junitxml=...``)
  - coverage.py SQLite DB produced with ``--cov-context=test`` (``.coverage``)

Writes four files into ``--output-dir``:
  1. ``durations.json``: top-N (and full sorted lists) by test, test file, and
     source module.
  2. ``source-to-tests.json``: mapping from each ``src/`` file to covering
     test files.
  3. ``audit-candidates.json``: test files (and single tests) whose covered-line
     set is a subset of another test file's (or single test's) covered-line set.
  4. ``summary.md``: concise human-readable Markdown summary of all three
     reports.

Fails loudly (non-zero exit) if inputs are missing/empty or if the coverage DB
contains no non-empty per-test contexts (e.g. if xdist combination dropped them).
"""

from __future__ import annotations

import argparse
import json
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import coverage
from static_test_imports import StaticImportData, collect_static_source_to_tests


@dataclass(frozen=True)
class TestCaseTiming:
    nodeid: str
    test_file: str
    duration_seconds: float
    outcome: str


@dataclass(frozen=True)
class CoverageContextData:
    src_to_test_files: dict[str, set[str]]
    test_file_masks: dict[str, int]
    nodeid_masks: dict[str, int]
    nodeid_to_src_files: dict[str, set[str]]
    all_src_files: list[str]
    # src/ files with lines that ran outside any test context (import time,
    # collection). A test that depends on them only through import is NOT in
    # src_to_test_files, so the map under-selects for changes to these files.
    src_files_with_uncontexted_lines: list[str] = field(default_factory=list)


@dataclass
class _TestFileStats:
    test_file: str
    duration_seconds: float = 0.0
    test_count: int = 0


@dataclass
class _SourceModuleStats:
    source_file: str
    total_touching_duration_seconds: float = 0.0
    proportional_duration_seconds: float = 0.0
    touching_test_count: int = 0
    touching_test_files: set[str] = field(default_factory=set)


def _normalize_repo_path(raw_path: str, repo_root: Path) -> str:
    """Normalize a measured file path to a repo-relative POSIX path."""
    candidate = Path(raw_path)
    try:
        resolved_root = repo_root.resolve()
        resolved_candidate = candidate.resolve()
        rel = resolved_candidate.relative_to(resolved_root)
        return rel.as_posix()
    except (OSError, ValueError):
        pass

    posix = raw_path.replace("\\", "/")
    root_posix = repo_root.as_posix().rstrip("/")
    if posix.lower().startswith(root_posix.lower() + "/"):
        return posix[len(root_posix) + 1 :]
    if "/src/" in posix:
        return "src/" + posix.split("/src/", 1)[1]
    return posix


def _classname_to_file_and_class(
    classname: str, repo_root: Path
) -> tuple[str, list[str]]:
    """Resolve pytest JUnit XML classname (dotted) to (test_file, class_parts)."""
    parts = [p for p in classname.split(".") if p]
    if not parts:
        return ("unknown", [])

    for idx in range(len(parts), 0, -1):
        candidate = repo_root.joinpath(*parts[: idx - 1], f"{parts[idx - 1]}.py")
        if candidate.is_file():
            return ("/".join(parts[:idx]) + ".py", parts[idx:])

    test_indices = [i for i, part in enumerate(parts) if part.startswith("test_")]
    if test_indices:
        split_at = test_indices[-1] + 1
        return ("/".join(parts[:split_at]) + ".py", parts[split_at:])

    return ("/".join(parts) + ".py", [])


def parse_junit_xml(junit_path: Path, repo_root: Path) -> list[TestCaseTiming]:
    """Parse pytest JUnit XML into normalized TestCaseTiming records."""
    if not junit_path.is_file():
        raise FileNotFoundError(f"JUnit XML file not found: {junit_path}")

    tree = ET.parse(junit_path)
    root = tree.getroot()
    cases: list[TestCaseTiming] = []
    # Classnames repeat across every test in a class; each lookup stats files.
    resolved_classnames: dict[str, tuple[str, list[str]]] = {}

    for elem in root.iter("testcase"):
        classname = elem.attrib.get("classname", "")
        name = elem.attrib.get("name", "")
        duration = float(elem.attrib.get("time", "0") or "0")

        if elem.find("failure") is not None:
            outcome = "failure"
        elif elem.find("error") is not None:
            outcome = "error"
        elif elem.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"

        if classname not in resolved_classnames:
            resolved_classnames[classname] = _classname_to_file_and_class(
                classname, repo_root
            )
        test_file, class_parts = resolved_classnames[classname]
        nodeid_parts = (
            [test_file, *class_parts, name] if name else [test_file, *class_parts]
        )
        nodeid = "::".join(nodeid_parts)
        cases.append(
            TestCaseTiming(
                nodeid=nodeid,
                test_file=test_file,
                duration_seconds=duration,
                outcome=outcome,
            )
        )

    if not cases:
        raise ValueError(f"JUnit XML contains zero <testcase> entries: {junit_path}")
    return cases


def _strip_context_phase(context: str) -> str:
    """Strip pytest-cov's '|run', '|setup', or '|teardown' suffix."""
    if "|" in context:
        nodeid, _, phase = context.rpartition("|")
        if phase in {"run", "setup", "teardown"}:
            return nodeid
    return context


def load_coverage_contexts(coverage_db: Path, repo_root: Path) -> CoverageContextData:
    """Read coverage.py DB and extract per-test and per-file src/ coverage bitmasks."""
    if not coverage_db.is_file():
        raise FileNotFoundError(f"Coverage DB not found: {coverage_db}")

    cov_data = coverage.CoverageData(basename=str(coverage_db))
    cov_data.read()

    measured_contexts = {c for c in cov_data.measured_contexts() if c}
    if not measured_contexts:
        raise ValueError(
            f"Coverage DB has no non-empty per-test contexts: {coverage_db}. "
            "Ensure pytest ran with --cov-context=test and xdist combination "
            "preserved contexts."
        )

    measured_files = cov_data.measured_files()
    if not measured_files:
        raise ValueError(f"Coverage DB has no measured files: {coverage_db}")

    src_to_test_files: dict[str, set[str]] = defaultdict(set)
    test_file_masks: dict[str, int] = defaultdict(int)
    nodeid_masks: dict[str, int] = defaultdict(int)
    nodeid_to_src_files: dict[str, set[str]] = defaultdict(set)
    all_src_files: set[str] = set()
    uncontexted_src_files: set[str] = set()

    line_bit_index = 0

    for raw_file in sorted(measured_files):
        norm_file = _normalize_repo_path(raw_file, repo_root)
        if not norm_file.startswith("src/"):
            continue
        all_src_files.add(norm_file)

        contexts_by_line = cov_data.contexts_by_lineno(raw_file)
        for _lineno, contexts in sorted(contexts_by_line.items()):
            non_empty = [c for c in contexts if c]
            if len(non_empty) < len(contexts):
                uncontexted_src_files.add(norm_file)
            if not non_empty:
                continue
            bit = 1 << line_bit_index
            line_bit_index += 1
            for ctx in non_empty:
                # Only the file part is a path; parametrize ids after "::" may
                # contain literal backslashes (e.g. "[\udce9]") and must stay.
                path_part, sep, rest = _strip_context_phase(ctx).partition("::")
                test_file = path_part.replace("\\", "/")
                nodeid = test_file + sep + rest
                src_to_test_files[norm_file].add(test_file)
                nodeid_masks[nodeid] |= bit
                test_file_masks[test_file] |= bit
                nodeid_to_src_files[nodeid].add(norm_file)

    if not all_src_files:
        raise ValueError(f"Coverage DB recorded no files under src/: {coverage_db}")
    if line_bit_index == 0:
        raise ValueError(
            "Coverage DB has contexts but no src/ lines attributed to any "
            f"test context: {coverage_db}"
        )

    return CoverageContextData(
        src_to_test_files=dict(src_to_test_files),
        test_file_masks=dict(test_file_masks),
        nodeid_masks=dict(nodeid_masks),
        nodeid_to_src_files=dict(nodeid_to_src_files),
        all_src_files=sorted(all_src_files),
        src_files_with_uncontexted_lines=sorted(uncontexted_src_files),
    )


MAX_SUPERSET_GROUPS = 10


def _find_subset_candidates(
    masks: dict[str, int],
    *,
    superset_list_name: str,
) -> list[dict[str, object]]:
    """Group identical non-empty bitmasks, then find subset/equal-group candidates."""
    groups_by_mask: dict[int, list[str]] = defaultdict(list)
    for name, mask in masks.items():
        if mask != 0:
            groups_by_mask[mask].append(name)

    groups = [
        (mask, mask.bit_count(), sorted(names))
        for mask, names in groups_by_mask.items()
    ]
    groups.sort(key=lambda g: (g[1], g[2][0]))

    candidates: list[dict[str, object]] = []
    for i, (mask_a, count_a, members_a) in enumerate(groups):
        supersets: list[dict[str, object]] = []
        for j in range(i + 1, len(groups)):
            mask_b, count_b, members_b = groups[j]
            if count_b <= count_a:
                continue
            if (mask_a & mask_b) == mask_a:
                supersets.append(
                    {
                        "members": members_b,
                        "member_count": len(members_b),
                        "covered_line_count": count_b,
                    }
                )
        if len(members_a) >= 2 or supersets:
            supersets.sort(
                key=lambda s: (-int(s["covered_line_count"]), str(s["members"][0]))
            )
            candidates.append(
                {
                    "covered_line_count": count_a,
                    "member_count": len(members_a),
                    "members": members_a,
                    "superset_count": len(supersets),
                    superset_list_name: supersets[:MAX_SUPERSET_GROUPS],
                }
            )

    candidates.sort(key=lambda c: (int(c["covered_line_count"]), str(c["members"][0])))
    return candidates


# The CI job tolerates a few failed tests (wall-clock budgets break under
# per-test coverage tracing). Above this share the coverage map is too
# incomplete to guide test selection, so the analysis fails instead.
MAX_FAILED_TEST_RATIO = 0.01


def check_failed_tests(cases: list[TestCaseTiming]) -> list[str]:
    """Return failed/errored nodeids; raise if too many to trust the reports."""
    failed = sorted(c.nodeid for c in cases if c.outcome in {"failure", "error"})
    if len(failed) > MAX_FAILED_TEST_RATIO * len(cases):
        sample = ", ".join(failed[:5])
        raise ValueError(
            f"{len(failed)} of {len(cases)} tests failed or errored (e.g. {sample}); "
            "too many for the coverage map to be complete."
        )
    return failed


# Above this share of coverage-context nodeids missing from the JUnit XML, the
# two sources are joined wrongly and per-module durations would silently shrink.
MAX_UNJOINED_CONTEXT_RATIO = 0.01


def check_nodeid_join(
    cases: list[TestCaseTiming], cov_ctx: CoverageContextData
) -> list[str]:
    """Return coverage nodeids absent from JUnit; raise if too many to trust.

    Checked coverage -> JUnit only: every coverage context is a real test that
    must appear in JUnit, whereas a JUnit test that touches no src/ line has no
    coverage context at all, so the reverse direction proves nothing.
    """
    junit_nodeids = {c.nodeid for c in cases}
    unjoined = sorted(set(cov_ctx.nodeid_masks) - junit_nodeids)
    if unjoined and len(unjoined) > MAX_UNJOINED_CONTEXT_RATIO * len(
        cov_ctx.nodeid_masks
    ):
        sample = ", ".join(unjoined[:5])
        raise ValueError(
            f"{len(unjoined)} of {len(cov_ctx.nodeid_masks)} coverage test contexts "
            f"have no matching JUnit test (e.g. {sample}); the JUnit classname to "
            "nodeid reconstruction does not match pytest's nodeids."
        )
    return unjoined


def build_reports(
    cases: list[TestCaseTiming],
    cov_ctx: CoverageContextData,
    static_imports: StaticImportData | None = None,
    *,
    top_n: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], str]:
    """Assemble the three JSON reports and Markdown summary."""
    if static_imports is None:
        static_imports = StaticImportData(
            src_to_test_files={}, all_src_files=[], all_test_files=[]
        )
    # 1. Durations
    sorted_tests = sorted(cases, key=lambda c: (-c.duration_seconds, c.nodeid))
    by_test_file: dict[str, _TestFileStats] = {}
    for c in cases:
        entry = by_test_file.setdefault(
            c.test_file, _TestFileStats(test_file=c.test_file)
        )
        entry.duration_seconds = round(entry.duration_seconds + c.duration_seconds, 6)
        entry.test_count += 1

    sorted_test_files = [
        {
            "test_file": item.test_file,
            "duration_seconds": item.duration_seconds,
            "test_count": item.test_count,
        }
        for item in sorted(
            by_test_file.values(),
            key=lambda item: (-item.duration_seconds, item.test_file),
        )
    ]

    by_source_module: dict[str, _SourceModuleStats] = {
        src: _SourceModuleStats(source_file=src) for src in cov_ctx.all_src_files
    }
    for c in cases:
        touched = cov_ctx.nodeid_to_src_files.get(c.nodeid, set())
        if not touched:
            continue
        share = c.duration_seconds / len(touched)
        for src in touched:
            mod = by_source_module[src]
            mod.total_touching_duration_seconds = round(
                mod.total_touching_duration_seconds + c.duration_seconds, 6
            )
            mod.proportional_duration_seconds = round(
                mod.proportional_duration_seconds + share, 6
            )
            mod.touching_test_count += 1
            mod.touching_test_files.add(c.test_file)

    sorted_source_modules = [
        {
            "source_file": m.source_file,
            "total_touching_duration_seconds": m.total_touching_duration_seconds,
            "proportional_duration_seconds": m.proportional_duration_seconds,
            "touching_test_count": m.touching_test_count,
            "touching_test_file_count": len(m.touching_test_files),
        }
        for m in sorted(
            by_source_module.values(),
            key=lambda item: (-item.total_touching_duration_seconds, item.source_file),
        )
    ]

    total_duration = round(sum(c.duration_seconds for c in cases), 6)
    all_tests = [
        {
            "nodeid": c.nodeid,
            "test_file": c.test_file,
            "duration_seconds": round(c.duration_seconds, 6),
            "outcome": c.outcome,
        }
        for c in sorted_tests
    ]
    durations_report = {
        "schema_version": 1,
        "top_n": top_n,
        "total_tests": len(cases),
        "total_test_files": len(by_test_file),
        "total_source_modules": len(cov_ctx.all_src_files),
        "total_duration_seconds": total_duration,
        "top_tests": all_tests[:top_n],
        "top_test_files": sorted_test_files[:top_n],
        "top_source_modules": sorted_source_modules[:top_n],
        "all_tests": all_tests,
        "all_test_files": sorted_test_files,
        "all_source_modules": sorted_source_modules,
    }

    # 2. Source-to-tests mapping (union of dynamic coverage + static AST imports)
    mapping_src_files = sorted(
        set(cov_ctx.all_src_files) | set(static_imports.all_src_files)
    )
    mapping_dict: dict[str, list[dict[str, str]]] = {}
    dyn_counts: list[int] = []
    imp_counts: list[int] = []
    union_counts: list[int] = []
    for src in mapping_src_files:
        dyn_tests = cov_ctx.src_to_test_files.get(src, set())
        imp_tests = static_imports.src_to_test_files.get(src, set())
        entries: list[dict[str, str]] = []
        for tf in sorted(dyn_tests | imp_tests):
            if tf in dyn_tests and tf in imp_tests:
                origin = "both"
            elif tf in dyn_tests:
                origin = "dynamic"
            else:
                origin = "import"
            entries.append({"test_file": tf, "source": origin})
        mapping_dict[src] = entries
        dyn_counts.append(len(dyn_tests))
        imp_counts.append(len(imp_tests))
        union_counts.append(len(entries))

    unreferenced_sources = [src for src, entries in mapping_dict.items() if not entries]
    all_mapped_test_files = sorted(
        {entry["test_file"] for entries in mapping_dict.values() for entry in entries}
    )
    mapping_report = {
        "schema_version": 2,
        "source_file_count": len(mapping_src_files),
        "covered_source_file_count": len(mapping_src_files) - len(unreferenced_sources),
        "mapped_test_file_count": len(all_mapped_test_files),
        "unreferenced_source_files": unreferenced_sources,
        # Import-time lines carry no test context: dynamic coverage alone misses
        # tests that depend on these files only through import.
        "source_files_with_uncontexted_lines": (
            cov_ctx.src_files_with_uncontexted_lines
        ),
        "source_to_test_files": mapping_dict,
    }

    # 3. Audit candidates
    all_junit_test_files = sorted({c.test_file for c in cases})
    zero_src_test_files = sorted(
        tf for tf in all_junit_test_files if cov_ctx.test_file_masks.get(tf, 0) == 0
    )
    test_file_candidates = _find_subset_candidates(
        cov_ctx.test_file_masks,
        superset_list_name="superset_test_files",
    )
    single_test_candidates = _find_subset_candidates(
        cov_ctx.nodeid_masks,
        superset_list_name="superset_tests",
    )
    test_file_candidate_count = sum(
        int(c["member_count"]) for c in test_file_candidates
    )
    single_test_candidate_count = sum(
        int(c["member_count"]) for c in single_test_candidates
    )
    audit_report = {
        "schema_version": 2,
        "max_superset_groups": MAX_SUPERSET_GROUPS,
        "test_file_candidate_group_count": len(test_file_candidates),
        "test_file_candidate_count": test_file_candidate_count,
        "single_test_candidate_group_count": len(single_test_candidates),
        "single_test_candidate_count": single_test_candidate_count,
        "zero_src_coverage_test_file_count": len(zero_src_test_files),
        "zero_src_coverage_test_files": zero_src_test_files,
        "test_file_candidates": test_file_candidates,
        "single_test_candidates": single_test_candidates,
    }

    # 4. Markdown summary
    md_lines = [
        "# Test Duration & Coverage Measurement Summary",
        "",
        f"- Total tests: {len(cases)} across {len(by_test_file)} test files",
        f"- Total cumulative test duration: {total_duration:.2f}s",
        f"- Measured `src/` files: {len(cov_ctx.all_src_files)}",
        "",
        f"## Top Slowest Tests (Top {min(top_n, len(sorted_tests))})",
        "",
        "| Rank | Duration (s) | Outcome | Node ID |",
        "|---:|---:|---|---|",
    ]
    for idx, c in enumerate(sorted_tests[:top_n], 1):
        md_lines.append(
            f"| {idx} | {c.duration_seconds:.3f} | {c.outcome} | `{c.nodeid}` |"
        )

    md_lines.extend(
        [
            "",
            f"## Top Slowest Test Files (Top {min(top_n, len(sorted_test_files))})",
            "",
            "| Rank | Duration (s) | Tests | Test File |",
            "|---:|---:|---:|---|",
        ]
    )
    for idx, tf in enumerate(sorted_test_files[:top_n], 1):
        dur = float(tf["duration_seconds"])
        md_lines.append(
            f"| {idx} | {dur:.3f} | {tf['test_count']} | `{tf['test_file']}` |"
        )

    mod_top = min(top_n, len(sorted_source_modules))
    md_lines.extend(
        [
            "",
            f"## Top Source Modules by Touching Test Duration (Top {mod_top})",
            "",
            "| Rank | Touching (s) | Proportional (s) | Tests | Source File |",
            "|---:|---:|---:|---:|---|",
        ]
    )
    for idx, mod in enumerate(sorted_source_modules[:top_n], 1):
        md_lines.append(
            f"| {idx} | {float(mod['total_touching_duration_seconds']):.3f} | "
            f"{float(mod['proportional_duration_seconds']):.3f} | "
            f"{mod['touching_test_count']} | `{mod['source_file']}` |"
        )

    dyn_avg = statistics.mean(dyn_counts) if dyn_counts else 0.0
    dyn_median = statistics.median(dyn_counts) if dyn_counts else 0.0
    imp_avg = statistics.mean(imp_counts) if imp_counts else 0.0
    imp_median = statistics.median(imp_counts) if imp_counts else 0.0
    union_avg = statistics.mean(union_counts) if union_counts else 0.0
    union_median = statistics.median(union_counts) if union_counts else 0.0
    total_test_files = len(set(static_imports.all_test_files) | set(by_test_file))
    union_share = (union_avg / total_test_files) if total_test_files else 0.0

    md_lines.extend(
        [
            "",
            "## Source-to-Test Mapping",
            "",
            f"- Source files measured: {mapping_report['source_file_count']}",
            "- Source files covered by >= 1 test: "
            f"{mapping_report['covered_source_file_count']}",
            "- Distinct test files referenced: "
            f"{mapping_report['mapped_test_file_count']}",
            f"- Dynamic-only test files per source file: avg {dyn_avg:.2f}, "
            f"median {dyn_median:.1f}",
            f"- Import-only test files per source file: avg {imp_avg:.2f}, "
            f"median {imp_median:.1f}",
            f"- Union test files per source file: avg {union_avg:.2f}, "
            f"median {union_median:.1f} "
            f"({union_share:.1%} of {total_test_files} test files)",
            "",
            "## Audit Candidates (Subset Coverage)",
            "",
            f"- Test-file subset candidates: {test_file_candidate_count} "
            f"across {len(test_file_candidates)} groups",
            f"- Single-test subset candidates: {single_test_candidate_count} "
            f"across {len(single_test_candidates)} groups",
            "- Test files with zero `src/` coverage (excluded from subset check): "
            f"{len(zero_src_test_files)}",
        ]
    )
    if test_file_candidates:
        md_lines.extend(
            [
                "",
                "| Candidate Test File(s) | Members | Lines | "
                "Superset Test File Groups (sample) |",
                "|---|---:|---:|---|",
            ]
        )
        for cand in test_file_candidates[:top_n]:
            members_sample = ", ".join(f"`{m}`" for m in cand["members"][:3])
            if cand["member_count"] > 3:
                members_sample += f" (+{cand['member_count'] - 3})"
            supers = ", ".join(
                f"`{s['members'][0]}`"
                + (f" (+{s['member_count'] - 1})" if s["member_count"] > 1 else "")
                + f" ({s['covered_line_count']})"
                for s in cand["superset_test_files"][:3]
            )
            if not supers:
                supers = "-"
            md_lines.append(
                f"| {members_sample} | {cand['member_count']} | "
                f"{cand['covered_line_count']} | {supers} |"
            )

    return durations_report, mapping_report, audit_report, "\n".join(md_lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze pytest JUnit XML durations and coverage.py per-test contexts."
        )
    )
    parser.add_argument("--junit-xml", required=True, type=Path)
    parser.add_argument("--coverage-db", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-root", default=Path("."), type=Path)
    parser.add_argument("--top-n", default=20, type=int)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    cases = parse_junit_xml(args.junit_xml, repo_root)
    failed = check_failed_tests(cases)
    cov_ctx = load_coverage_contexts(args.coverage_db, repo_root)
    unjoined = check_nodeid_join(cases, cov_ctx)
    static_imports = collect_static_source_to_tests(repo_root)

    durations_report, mapping_report, audit_report, summary_md = build_reports(
        cases,
        cov_ctx,
        static_imports,
        top_n=args.top_n,
    )
    durations_report["coverage_nodeids_missing_from_junit"] = unjoined
    durations_report["failed_tests"] = failed

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("durations.json", durations_report),
        ("source-to-tests.json", mapping_report),
        ("audit-candidates.json", audit_report),
    ):
        (args.output_dir / name).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    (args.output_dir / "summary.md").write_text(summary_md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
