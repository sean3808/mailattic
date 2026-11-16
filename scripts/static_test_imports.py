"""Static import dependency scanner for tests -> src/mailattic/*.py.

Uses stdlib ``ast`` only to compute, for every ``tests/**/test_*.py`` file,
the transitive set of ``src/mailattic/**/*.py`` modules it reaches via:
  - the test file's own imports (including function-body imports)
  - every ``conftest.py`` in the test file's directory and ancestors up to ``tests/``
  - non-test helper modules inside ``tests/`` imported by any of those files
  - transitive imports (absolute and relative) within ``src/mailattic/``

Imports inside ``if TYPE_CHECKING:`` (or ``if typing.TYPE_CHECKING:``) blocks
are excluded because they do not execute at runtime.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StaticImportData:
    """Static import graph projection from src/ files to test files."""

    src_to_test_files: dict[str, set[str]]
    all_src_files: list[str]
    all_test_files: list[str]


def _is_type_checking_guard(test: ast.expr) -> bool:
    """Return True for ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _iter_runtime_import_nodes(
    tree: ast.AST,
) -> list[ast.Import | ast.ImportFrom]:
    """Collect Import/ImportFrom nodes, skipping ``if TYPE_CHECKING:`` bodies."""
    imports: list[ast.Import | ast.ImportFrom] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            continue
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            # Skip the TYPE_CHECKING body, but still visit any orelse branch.
            stack.extend(reversed(node.orelse))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return imports


def _ancestor_files(target: Path, stop_dir: Path, filename: str) -> list[Path]:
    """Return existing ``filename`` files from target's dir up to ``stop_dir``."""
    found: list[Path] = []
    current = target.parent
    while True:
        candidate = current / filename
        if candidate.is_file():
            found.append(candidate)
        if current == stop_dir or current == current.parent:
            break
        current = current.parent
    return found


class _ImportResolver:
    """Resolve and cache direct intra-repo imports for Python files."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.src_root = repo_root / "src"
        self.src_pkg_dir = self.src_root / "mailattic"
        self.tests_dir = repo_root / "tests"
        self.src_module_to_path: dict[str, Path] = {}
        self.test_module_to_path: dict[str, Path] = {}
        self._direct_cache: dict[Path, list[Path]] = {}
        self._index_modules()

    def _index_modules(self) -> None:
        if self.src_pkg_dir.is_dir():
            for path in sorted(self.src_pkg_dir.rglob("*.py")):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(self.src_root).with_suffix("").parts
                if rel_parts[-1] == "__init__":
                    mod_name = ".".join(rel_parts[:-1])
                    self.src_module_to_path[mod_name] = path
                    self.src_module_to_path[f"{mod_name}.__init__"] = path
                else:
                    self.src_module_to_path[".".join(rel_parts)] = path

        if self.tests_dir.is_dir():
            for path in sorted(self.tests_dir.rglob("*.py")):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(self.repo_root).with_suffix("").parts
                if rel_parts[-1] == "__init__":
                    self.test_module_to_path[".".join(rel_parts[:-1])] = path
                else:
                    self.test_module_to_path[".".join(rel_parts)] = path

                sub_parts = path.relative_to(self.tests_dir).with_suffix("").parts
                if sub_parts[-1] == "__init__":
                    if len(sub_parts) > 1:
                        self.test_module_to_path.setdefault(
                            ".".join(sub_parts[:-1]), path
                        )
                else:
                    self.test_module_to_path.setdefault(".".join(sub_parts), path)

    def _resolve_dotted(self, mod_name: str) -> list[Path]:
        if mod_name in self.src_module_to_path:
            target = self.src_module_to_path[mod_name]
            return [target, *_ancestor_files(target, self.src_pkg_dir, "__init__.py")]
        if mod_name in self.test_module_to_path:
            target = self.test_module_to_path[mod_name]
            return [target, *_ancestor_files(target, self.tests_dir, "__init__.py")]
        return []

    def _resolve_relative(self, current_file: Path, node: ast.ImportFrom) -> list[Path]:
        if node.level - 1 >= len(current_file.parents):
            return []
        base_dir = current_file.parents[node.level - 1]
        if current_file.is_relative_to(self.src_pkg_dir):
            stop_dir = self.src_pkg_dir
        elif current_file.is_relative_to(self.tests_dir):
            stop_dir = self.tests_dir
        else:
            return []

        if not (base_dir == stop_dir or base_dir.is_relative_to(stop_dir)):
            return []

        found: list[Path] = []
        if node.module:
            mod_parts = node.module.split(".")
            search_dir = base_dir.joinpath(*mod_parts)
            candidates = [
                base_dir.joinpath(*mod_parts[:-1], f"{mod_parts[-1]}.py"),
                search_dir / "__init__.py",
            ]
        else:
            search_dir = base_dir
            candidates = [base_dir / "__init__.py"]

        for alias in node.names:
            if alias.name != "*":
                candidates.append(search_dir / f"{alias.name}.py")
                candidates.append(search_dir / alias.name / "__init__.py")

        for cand in candidates:
            if cand.is_file():
                found.append(cand)
                found.extend(_ancestor_files(cand, stop_dir, "__init__.py"))
        return found

    def direct_imports(self, file_path: Path) -> list[Path]:
        cached = self._direct_cache.get(file_path)
        if cached is not None:
            return cached

        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            self._direct_cache[file_path] = []
            return []

        resolved: list[Path] = []
        seen: set[Path] = set()

        def _add(paths: list[Path]) -> None:
            for p in paths:
                if p not in seen:
                    seen.add(p)
                    resolved.append(p)

        for node in _iter_runtime_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add(self._resolve_dotted(alias.name))
            elif node.level > 0:
                _add(self._resolve_relative(file_path, node))
            elif node.module:
                _add(self._resolve_dotted(node.module))
                for alias in node.names:
                    if alias.name != "*":
                        _add(self._resolve_dotted(f"{node.module}.{alias.name}"))

        self._direct_cache[file_path] = resolved
        return resolved


def collect_static_source_to_tests(repo_root: Path) -> StaticImportData:
    """Scan ``tests/`` and ``src/mailattic/`` to build static import mapping."""
    resolved_root = repo_root.resolve()
    resolver = _ImportResolver(resolved_root)

    all_src_files = sorted(
        path.relative_to(resolved_root).as_posix()
        for path in resolver.src_module_to_path.values()
        if path.is_file()
    )
    # Deduplicate in case both mod and mod.__init__ mapped to __init__.py.
    all_src_files = sorted(set(all_src_files))

    test_files: list[Path] = []
    if resolver.tests_dir.is_dir():
        test_files = sorted(
            p for p in resolver.tests_dir.rglob("test_*.py") if p.is_file()
        )

    all_test_files = sorted(p.relative_to(resolved_root).as_posix() for p in test_files)
    src_to_test_files: dict[str, set[str]] = defaultdict(set)

    for test_path in test_files:
        test_rel = test_path.relative_to(resolved_root).as_posix()
        seeds = [
            test_path,
            *_ancestor_files(test_path, resolver.tests_dir, "conftest.py"),
        ]
        queue: deque[Path] = deque(seeds)
        visited: set[Path] = set(seeds)

        while queue:
            current = queue.popleft()
            if current.is_relative_to(resolver.src_pkg_dir):
                src_rel = current.relative_to(resolved_root).as_posix()
                src_to_test_files[src_rel].add(test_rel)
            for dep in resolver.direct_imports(current):
                if dep not in visited:
                    visited.add(dep)
                    queue.append(dep)

    return StaticImportData(
        src_to_test_files=dict(src_to_test_files),
        all_src_files=all_src_files,
        all_test_files=all_test_files,
    )
