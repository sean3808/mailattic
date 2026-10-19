from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sqlite3
import string
import tempfile
import textwrap
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import mcp.types as mcp_types
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from mailattic import mcp_delivery as mcp_delivery_module
from mailattic import mcp_readonly as mcp_readonly_module
from mailattic import mcp_server as mcp_server_module
from mailattic import mcp_writer as mcp_writer_module
from mailattic import query as query_module
from mailattic.datetime_bounds import format_invalid_date_message
from mailattic.identity import add_self_address
from mailattic.mcp_delivery import (
    MCP_RESPONSE_BUDGET_CHARS,
    LadderExhausted,
    measure_call_tool_result_chars,
    walk_ladder,
)
from mailattic.mcp_server import (
    create_server,
    render_message_body,
    shape_message_payload,
)
from mailattic.models import PstSource, build_normalized_message
from mailattic.storage import (
    ENV_VAR_DB,
    SYNC_STATE_KEY,
    connect_database,
    initialize_database,
    record_sync_run_end,
    record_sync_run_start,
)
from mailattic.storage import persist_message as storage_persist_message

_APPROVED_ERROR_BUILDERS = {"build_tool_error", "build_environment_error"}
_MAX_ELISION_LENGTH_DIGITS = 20
_WINDOWS_EXTENDED_PATH_MAX_CHARS = 32_767

#: The framework wraps a raised tool error's message as
#: ``f"Error executing tool {tool_name}: {message}"`` before assembling the
#: delivered error result, so the delivered message is longer than the one
#: this server builds. That addition is a counted term in the static bound
#: (spec > "Error results are bounded by construction", "The framework's
#: message prefix is a counted term"); leaving it out understates the
#: delivered length. Bounded statically by fixed text plus the longest
#: registered tool name. Why a budget comparison cannot establish this term,
#: and what does instead, is argued once in the docstring of
#: ``test_framework_error_prefix_term_covers_its_measured_addition``.
_FRAMEWORK_ERROR_PREFIX_TEMPLATE = "Error executing tool {tool_name}: "

#: Third tier of the response-size guarantee: the delivered length of the
#: framework's OWN argument-validation error, measured against the mcp
#: version this repository resolves, for a 160,000-character wrong-type
#: argument. The message is the framework's and neither construction point
#: can bound it, so this value is empirical -- it lives here rather than in
#: the requirement text, and it exists to fail loudly when the framework
#: changes its formatting rather than to state a guarantee.
_FRAMEWORK_VALIDATION_ERROR_MEASURED_CHARS = 455

#: Single source for the registered tool set. The static error bound is
#: computed at module scope, so this cannot live on the test class.
_REGISTERED_TOOL_NAMES = (
    "mail_search",
    "mail_show",
    "mail_thread",
    "mail_contact_history",
    "mail_attachments",
    "mail_sync_status",
    "mail_attachment_export",
)


def _framework_error_prefix_max_chars() -> int:
    return len(
        _FRAMEWORK_ERROR_PREFIX_TEMPLATE.format(
            tool_name=max(_REGISTERED_TOOL_NAMES, key=len)
        )
    )


def _call_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _is_static_string_expression(
    node: ast.AST | None,
    string_constants: set[str],
    *,
    allow_date_formatter: bool = False,
) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id in string_constants
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_static_string_expression(
            node.left, string_constants, allow_date_formatter=allow_date_formatter
        ) and _is_static_string_expression(
            node.right, string_constants, allow_date_formatter=allow_date_formatter
        )
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
            if not isinstance(value, ast.FormattedValue):
                return False
            if (
                not isinstance(value.value, ast.Name)
                or value.value.id not in string_constants
            ):
                return False
        return True
    return allow_date_formatter and (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "format_invalid_date_message"
    )


def _static_string_value(
    node: ast.AST | None,
    string_constants: dict[str, str],
    *,
    allow_date_formatter: bool = False,
) -> str | None:
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return string_constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_value(
            node.left, string_constants, allow_date_formatter=allow_date_formatter
        )
        right = _static_string_value(
            node.right, string_constants, allow_date_formatter=allow_date_formatter
        )
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if not isinstance(value, ast.FormattedValue) or value.conversion != -1:
                return None
            if value.format_spec is not None:
                return None
            if not isinstance(value.value, ast.Name):
                return None
            constant = string_constants.get(value.value.id)
            if constant is None:
                return None
            parts.append(constant)
        return "".join(parts)
    if (
        allow_date_formatter
        and isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "format_invalid_date_message"
        and len(node.args) >= 2
    ):
        value = _static_string_value(node.args[0], string_constants)
        parameter_name = _static_string_value(node.args[1], string_constants)
        expected_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "expected"),
            None,
        )
        expected = _static_string_value(expected_node, string_constants)
        if value is None or parameter_name is None or expected is None:
            return None
        return format_invalid_date_message(value, parameter_name, expected=expected)
    return None


def _module_string_values(tree: ast.Module) -> dict[str, str]:
    assignments: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = statement.value

    constants: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, value in assignments.items():
            if name not in constants:
                static_value = _static_string_value(value, constants)
                if static_value is not None:
                    constants[name] = static_value
                    changed = True
    return constants


def _module_string_constants(tree: ast.Module) -> set[str]:
    return set(_module_string_values(tree))


def _direct_return_values(function: ast.FunctionDef | ast.AsyncFunctionDef):
    class ReturnCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.values: list[ast.expr | None] = []

        def visit_Return(self, node: ast.Return) -> None:
            self.values.append(node.value)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

    collector = ReturnCollector()
    collector.visit(function)
    return collector.values


#: Design.md > "Enforce with an AST guard over the server module source":
#: the boundary is an import allowlist, not a list of the five call sites
#: that exist today. These are the database-free helpers the server module
#: is still allowed to import from the data-access modules.
_DATA_ACCESS_MODULE_LEAVES = {"query", "contact_history", "storage"}
_ALLOWED_DATA_ACCESS_NAMES = {
    "SearchFilters",
    "ThreadIdentifierError",
    "render_text",
    "strip_quote_tail",
    "parse_date_bound",
    "resolve_database_default",
}


def _find_read_only_boundary_violations(source: str) -> list[str]:
    """Report every import-boundary and ``read_only``-keyword violation.

    Three properties, per design.md's "Enforce with an AST guard over the
    server module source": (1) every name imported from the query,
    contact-history, or storage modules is on the database-free allowlist;
    (2) none of those three modules is imported as a module -- neither as
    ``import mailattic.query`` nor as
    ``from mailattic import query`` nor as ``from . import
    query`` -- since any of those forms lets a later line reach a data
    function through a qualified or aliased attribute access; (3) no call
    anywhere in the module passes a ``read_only`` keyword argument. An empty
    return means the module respects the boundary.
    """
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name.split(".")
                if dotted[-1] in _DATA_ACCESS_MODULE_LEAVES and dotted[0] in (
                    "mailattic",
                    *_DATA_ACCESS_MODULE_LEAVES,
                ):
                    violations.append(
                        f"line {node.lineno}: module import of {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            module_leaf = module.split(".")[-1] if module else None
            imports_names_from_submodule = (
                module_leaf in _DATA_ACCESS_MODULE_LEAVES
                and (node.level >= 1 or (module is not None and "." in module))
            )
            imports_submodule_as_name = (
                module is None and node.level >= 1
            ) or module == "mailattic"
            if imports_names_from_submodule:
                for alias in node.names:
                    if alias.name not in _ALLOWED_DATA_ACCESS_NAMES:
                        violations.append(
                            f"line {node.lineno}: disallowed import "
                            f"{alias.name!r} from {module!r}"
                        )
            elif imports_submodule_as_name:
                for alias in node.names:
                    if alias.name in _DATA_ACCESS_MODULE_LEAVES:
                        violations.append(
                            f"line {node.lineno}: submodule {alias.name!r} "
                            "imported as a name, enabling qualified access"
                        )
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "read_only":
                    violations.append(
                        f"line {node.lineno}: read_only keyword argument present"
                    )
    return violations


def _find_reader_read_only_violations(source: str) -> list[str]:
    """Report every violation of the read-only accessor module's own boundary.

    The server module carries no ``read_only`` keyword at all -- the
    accessor binds it once. The accessor module is the opposite: every call
    it makes into a data-access function MUST carry an explicit
    ``read_only=True`` keyword, because that is the only place the setting
    is actually bound. Three properties, mirroring
    ``_find_read_only_boundary_violations`` but inverted for the accessor's
    own source: (1) every call to a data-access function imported from the
    query, contact-history, or storage modules (any name not on the
    database-free allowlist) passes ``read_only`` as an explicit keyword
    whose value is the literal ``True`` -- a missing keyword or a
    non-``True`` value is a violation, which is what catches a future
    method that forgets to bind it; (2) none of those three modules is
    imported as a module, for the same qualified-access reason as the
    server guard; (3) as a direct consequence of (1), a stray
    ``read_only=False`` anywhere in the module is also reported. An empty
    return means the accessor module respects its own boundary.
    """
    tree = ast.parse(source)
    violations: list[str] = []
    guarded_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name.split(".")
                if dotted[-1] in _DATA_ACCESS_MODULE_LEAVES and dotted[0] in (
                    "mailattic",
                    *_DATA_ACCESS_MODULE_LEAVES,
                ):
                    violations.append(
                        f"line {node.lineno}: module import of {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            module_leaf = module.split(".")[-1] if module else None
            imports_names_from_submodule = (
                module_leaf in _DATA_ACCESS_MODULE_LEAVES
                and (node.level >= 1 or (module is not None and "." in module))
            )
            imports_submodule_as_name = (
                module is None and node.level >= 1
            ) or module == "mailattic"
            if imports_names_from_submodule:
                for alias in node.names:
                    if alias.name not in _ALLOWED_DATA_ACCESS_NAMES:
                        guarded_names.add(alias.asname or alias.name)
            elif imports_submodule_as_name:
                for alias in node.names:
                    if alias.name in _DATA_ACCESS_MODULE_LEAVES:
                        violations.append(
                            f"line {node.lineno}: submodule {alias.name!r} "
                            "imported as a name, enabling qualified access"
                        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        read_only_keyword = next(
            (keyword for keyword in node.keywords if keyword.arg == "read_only"),
            None,
        )
        is_literal_true = read_only_keyword is not None and (
            isinstance(read_only_keyword.value, ast.Constant)
            and read_only_keyword.value.value is True
        )
        if read_only_keyword is not None and not is_literal_true:
            violations.append(
                f"line {node.lineno}: read_only keyword is not the literal True"
            )

        called_name = node.func.id if isinstance(node.func, ast.Name) else None
        if called_name in guarded_names and read_only_keyword is None:
            violations.append(
                f"line {node.lineno}: call to {called_name!r} is missing an "
                "explicit read_only=True keyword"
            )

    return violations


#: Issue #28 deliberately opened a disk-write entry into the MCP server
#: (attachment export). Round 3 of code review showed a blacklist of
#: bypass shapes cannot converge -- Python's reflection surface has no
#: enumerable ceiling (``importlib``, ``__import__``, ``builtins.__import__``,
#: ``sys.modules``, ``__globals__``, ``vars()``...); every round finds one
#: more shape. The 2026-09-24 decision (issue #28) replaced it with an
#: allowlist: every import in the server module must match one of these two
#: sets exactly, and any reflection primitive is banned outright.
#:
#: Threat model this guard actually covers: an author (human or agent) who
#: writes an ordinary import statement reaching attachment export from the
#: wrong module. It is NOT a sandbox against deliberate obfuscation --
#: deliberate obfuscation is out of this guard's scope by design, handled
#: instead by issue #28's explicit ban plus the mandatory cross-model review
#: this anchor file itself requires. Known unguarded limit, stated rather
#: than patched: re-implementing the export logic with no import at all
#: (reading ``storage_path`` off a row already in hand and writing it with
#: ``pathlib.Path.write_bytes`` -- both already allowlisted for ordinary
#: use) reaches the disk with zero imports this guard can see.
_RULE_IMPORT_ALLOWLIST = "import_allowlist"
_RULE_REFLECTION = "reflection"

#: Exact external module names. A bare `import x` or `from x import ...`
#: whose module string is not in this set is not in the allowlist.
_EXTERNAL_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "__future__",
        "argparse",
        "sqlite3",
        "collections.abc",
        "datetime",
        "pathlib",
        "typing",
        "mcp.server",
        "mcp.types",
        "pydantic",
    }
)

#: Project-internal imports: exact (direct submodule -> allowed name set)
#: pairs. The key is the full submodule path under the package, so only
#: `from .query import ...` / `from mailattic.query import ...`
#: match "query" -- `from .other.query import ...` or `from ..query import
#: ...` do not. Whole-module import of an internal module -- `import
#: mailattic.x`, `from . import x` -- is never allowed; only a
#: named from-import of a listed name is.
_INTERNAL_IMPORT_ALLOWLIST: dict[str, frozenset[str]] = {
    "contact_history": frozenset({"parse_date_bound"}),
    "datetime_bounds": frozenset(
        {
            "format_invalid_date_message",
            "normalize_optional_date_text",
            "whole_day_bound",
        }
    ),
    "mcp_delivery": frozenset(
        {
            "ERROR_ECHO_CAP_CHARS",
            "MCP_RESPONSE_BUDGET_CHARS",
            "LadderExhausted",
            "build_call_tool_result",
            "build_environment_error",
            "build_tool_error",
            "cap_error_echo",
            "measure_call_tool_result_chars",
            "walk_ladder",
        }
    ),
    "mcp_readonly": frozenset({"ReadOnlyMailReader"}),
    "mcp_writer": frozenset({"MailWriter"}),
    "query": frozenset(
        {"SearchFilters", "ThreadIdentifierError", "render_text", "strip_quote_tail"}
    ),
    "storage": frozenset({"resolve_database_default"}),
}

#: Bare names banned regardless of where they came from: each is a way to
#: reach an object without writing an ordinary import for it.
_BANNED_REFLECTION_NAMES = frozenset(
    {
        "__import__",
        "__builtins__",
        "getattr",
        "setattr",
        "delattr",
        "eval",
        "exec",
        "compile",
        "globals",
        "vars",
        "locals",
    }
)


def _internal_import_key(node: ast.ImportFrom) -> str | None:
    """Spell an internal from-import's module as its path under the
    package (`.query` and `mailattic.query` both -> "query";
    `..query` -> "..query"), or None for an external module. Keys that are
    not a listed direct submodule simply miss the allowlist."""
    module = node.module or ""
    if node.level >= 1:
        return "." * (node.level - 1) + module
    prefix = "mailattic."
    if module.startswith(prefix):
        return module[len(prefix) :]
    if module == "mailattic":
        return ""
    return None


def _find_import_allowlist_violations(tree: ast.AST) -> list[str]:
    """Every ``ast.Import``/``ast.ImportFrom`` node must match
    ``_EXTERNAL_IMPORT_ALLOWLIST`` or ``_INTERNAL_IMPORT_ALLOWLIST`` exactly.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "mailattic" or (
                    alias.name not in _EXTERNAL_IMPORT_ALLOWLIST
                ):
                    violations.append(
                        f"line {node.lineno}: import {alias.name!r} not in "
                        "import allowlist"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # `from . import x` (or `from .. import x`): binds a
                # submodule as a module object -- a whole-module import by
                # another spelling, never allowed.
                for alias in node.names:
                    violations.append(
                        f"line {node.lineno}: whole-module import of "
                        f"{alias.name!r} not in import allowlist"
                    )
                continue
            internal_key = _internal_import_key(node)
            if internal_key is not None:
                allowed_names = _INTERNAL_IMPORT_ALLOWLIST.get(
                    internal_key, frozenset()
                )
                for alias in node.names:
                    if alias.name not in allowed_names:
                        violations.append(
                            f"line {node.lineno}: {alias.name!r} from "
                            f"{node.module!r} not in import allowlist"
                        )
            elif node.module not in _EXTERNAL_IMPORT_ALLOWLIST:
                violations.append(
                    f"line {node.lineno}: import from {node.module!r} not "
                    "in import allowlist"
                )
    return violations


def _find_reflection_violations(tree: ast.AST) -> list[str]:
    """Ban any dunder attribute access (``__globals__``, ``__dict__``, ...)
    and any bare use of a name in ``_BANNED_REFLECTION_NAMES``."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
        ):
            violations.append(
                f"line {node.lineno}: reflection via dunder attribute {node.attr!r}"
            )
        elif isinstance(node, ast.Name) and node.id in _BANNED_REFLECTION_NAMES:
            violations.append(f"line {node.lineno}: reflection via name {node.id!r}")
    return violations


def _find_attachment_export_entry_violations(
    source: str, *, disabled_rules: frozenset[str] = frozenset()
) -> list[str]:
    """Report every allowlist or reflection violation in *source*.
    ``disabled_rules`` is a test-only knob (PC-096: prove each rule
    actually discriminates, not just that the combined check is
    non-empty) -- production callers never pass it.
    """
    tree = ast.parse(source)
    violations: list[str] = []
    if _RULE_IMPORT_ALLOWLIST not in disabled_rules:
        violations.extend(_find_import_allowlist_violations(tree))
    if _RULE_REFLECTION not in disabled_rules:
        violations.extend(_find_reflection_violations(tree))
    return violations


def _declared_attachment_export_imports(
    tree: ast.AST,
) -> tuple[set[str], set[tuple[str, str]]]:
    """Extract every import actually present, split the same way the
    allowlist is: external module names, and internal (submodule path, name)
    pairs. Used only by the bidirectional-equality test, which pairs this
    against ``_EXTERNAL_IMPORT_ALLOWLIST`` / ``_INTERNAL_IMPORT_ALLOWLIST``
    to prove the allowlist cannot silently accumulate slack -- removing an
    import from the real module must turn that test red, not leave it
    green by omission.
    """
    external: set[str] = set()
    internal: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                external.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            internal_key = _internal_import_key(node)
            if internal_key is not None:
                for alias in node.names:
                    internal.add((internal_key, alias.name))
            else:
                external.add(node.module)
    return external, internal


def _assert_error_construction_paths(source: str) -> None:
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    builder_returning_functions: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, function in functions.items():
            values = _direct_return_values(function)
            if (
                values
                and all(
                    (builder_name := _call_name(value))
                    in _APPROVED_ERROR_BUILDERS | builder_returning_functions
                    for value in values
                )
                and name not in builder_returning_functions
            ):
                builder_returning_functions.add(name)
                changed = True

    failures: list[str] = []
    allowed = _APPROVED_ERROR_BUILDERS | builder_returning_functions
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        if _call_name(node.exc) not in allowed:
            rendered = ast.unparse(node.exc)
            failures.append(f"line {node.lineno}: rejected raise {rendered}")
    if failures:
        raise AssertionError(
            "Every server error must be raised by an approved builder;\n"
            + "\n".join(failures)
        )

    string_constants = _module_string_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        builder_name = _call_name(node)
        if builder_name not in _APPROVED_ERROR_BUILDERS:
            continue

        template_index = 1 if builder_name == "build_tool_error" else 2
        if len(node.args) <= template_index:
            failures.append(
                f"line {node.lineno}: {builder_name} has no template argument"
            )
        elif not _is_static_string_expression(
            node.args[template_index],
            string_constants,
            allow_date_formatter=True,
        ):
            failures.append(
                f"line {node.lineno}: {builder_name} template is not static text"
            )

        if builder_name == "build_environment_error" and (
            len(node.args) < 2
            or not (
                isinstance(node.args[1], ast.Name)
                and node.args[1].id == "resolved_database_path"
            )
        ):
            failures.append(
                f"line {node.lineno}: {builder_name} uncapped argument is not "
                "resolved_database_path"
            )
    if failures:
        raise AssertionError(
            "Every server error builder must use static templates and the approved "
            "environment path;\n" + "\n".join(failures)
        )

    capped_fragment_max = (
        mcp_delivery_module.ERROR_ECHO_CAP_CHARS
        + len(mcp_delivery_module._ERROR_ECHO_ELISION_MARKER)
        + len(", ")
        + _MAX_ELISION_LENGTH_DIGITS
        + len(" chars total)")
    )
    formatter = string.Formatter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        builder_name = _call_name(node)
        if builder_name not in _APPROVED_ERROR_BUILDERS:
            continue
        template_index = 1 if builder_name == "build_tool_error" else 2
        template = None
        if len(node.args) > template_index:
            template = _static_string_value(
                node.args[template_index],
                _module_string_values(tree),
                allow_date_formatter=True,
            )
        if template is None:
            continue

        fragment_names = {keyword.arg for keyword in node.keywords}
        if None in fragment_names:
            failures.append(f"line {node.lineno}: **error fragments are not allowed")
            continue
        fragment_names.discard(None)
        fields: list[str] = []
        fixed_template_length = 0
        try:
            parsed_template = list(formatter.parse(template))
        except ValueError as exc:
            failures.append(f"line {node.lineno}: invalid builder template: {exc}")
            continue
        for literal, field_name, format_spec, conversion in parsed_template:
            fixed_template_length += len(literal)
            if field_name is None:
                continue
            if format_spec or conversion:
                failures.append(
                    f"line {node.lineno}: formatted fields must have no conversion "
                    "or format specifier"
                )
            if not field_name.isidentifier():
                failures.append(
                    f"line {node.lineno}: field {field_name!r} is not a simple "
                    "fragment name"
                )
            fields.append(field_name)

        expected_fragments = set(fields)
        if builder_name == "build_environment_error":
            expected_fragments.discard("database_path")
        if fragment_names != expected_fragments:
            failures.append(
                f"line {node.lineno}: template fragments "
                f"{sorted(expected_fragments)!r} "
                f"do not match keyword fragments {sorted(fragment_names)!r}"
            )

        worst_message_length = fixed_template_length
        naive_message_length = fixed_template_length
        worst_fragment_values: dict[str, str] = {}
        for field_name in fields:
            if (
                builder_name == "build_environment_error"
                and field_name == "database_path"
            ):
                worst_fragment_length = _WINDOWS_EXTENDED_PATH_MAX_CHARS
                worst_fragment_value = "\\" * _WINDOWS_EXTENDED_PATH_MAX_CHARS
            else:
                worst_fragment_length = capped_fragment_max
                worst_fragment_value = (
                    "\x00" * mcp_delivery_module.ERROR_ECHO_CAP_CHARS
                    + mcp_delivery_module._ERROR_ECHO_ELISION_MARKER
                    + ", "
                    + "9" * _MAX_ELISION_LENGTH_DIGITS
                    + " chars total)"
                )
            worst_message_length += worst_fragment_length
            naive_message_length += capped_fragment_max
            worst_fragment_values[field_name] = worst_fragment_value

        worst_message = template.format(**worst_fragment_values)
        if len(worst_message) != worst_message_length:
            failures.append(
                f"line {node.lineno}: assembled worst-case message length does "
                "not match the template-plus-fragments formula"
            )
        # Delivered envelope reached through the PUBLIC result constructor.
        # The 1.x route used the lowlevel server's private
        # `_make_error_result` and unwrapped a root model; 2.x removed both,
        # and depending on a private builder is what made this computation
        # break on a generation change in the first place (spec > "The
        # computed bound is reached without a private framework builder").
        # The prefix the framework adds to a raised tool error is counted
        # here rather than omitted -- omitting it bounds the message this
        # server assembled, not the delivered result.
        worst_delivered_message = (
            "E" * _framework_error_prefix_max_chars() + worst_message
        )
        error_result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=worst_delivered_message)],
            isError=True,
        )
        worst_delivered_length = measure_call_tool_result_chars(error_result)
        if worst_delivered_length > MCP_RESPONSE_BUDGET_CHARS:
            failures.append(
                f"line {node.lineno}: worst-case delivered error length "
                f"{worst_delivered_length} exceeds {MCP_RESPONSE_BUDGET_CHARS}"
            )
        if worst_delivered_length <= worst_message_length:
            failures.append(
                f"line {node.lineno}: delivered bound omitted JSON expansion or "
                "the framework error envelope"
            )
        if (
            builder_name == "build_environment_error"
            and worst_message_length <= naive_message_length
        ):
            failures.append(
                f"line {node.lineno}: environment path was counted at the "
                "ordinary capped-fragment bound"
            )
    if failures:
        raise AssertionError(
            "Every builder must have a computed delivered-size bound;\n"
            + "\n".join(failures)
        )


class McpServerTests(unittest.TestCase):
    def _unwrap_tool_manager_result(self, result: mcp_types.CallToolResult):
        structured = result.structured_content
        if structured is None:
            return result
        if set(structured) == {"result"}:
            return structured["result"]
        return structured

    def _call_mail_search(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_search", arguments))
        )

    def _call_mail_show(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_show", arguments))
        )

    def _call_mail_thread(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_thread", arguments))
        )

    def _call_mail_contact_history(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_contact_history", arguments))
        )

    def _call_mail_attachments(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_attachments", arguments))
        )

    def _call_mail_sync_status(self, server, **arguments):
        return self._unwrap_tool_manager_result(
            asyncio.run(server.call_tool("mail_sync_status", arguments))
        )

    def _seed_sync_row(
        self,
        database_path: Path,
        *,
        started_at: datetime,
        ended_at: datetime | None = None,
        outcome: str | None = None,
    ) -> None:
        """Put the sync_state row into a given state.

        Omit `ended_at` to leave the run in the `running` state; supply both
        `ended_at` and `outcome` for a terminal one.
        """
        connection = connect_database(database_path)
        try:
            record_sync_run_start(connection, started_at=started_at)
            if ended_at is not None:
                assert outcome is not None, "a terminal run needs an outcome"
                record_sync_run_end(connection, ended_at=ended_at, outcome=outcome)
        finally:
            connection.close()

    def _call_tool_success(
        self, server, name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        """Drive a delivery assertion through the public tool-call entry
        point (design.md > "Drive delivery assertions through the public
        tool-call entry point"). It returns the delivered `CallToolResult`
        for a successful call with no further assembly; our five tools
        never return the `InputRequiredResult` half of the union.
        """
        result = asyncio.run(server.call_tool(name, arguments))
        assert isinstance(result, mcp_types.CallToolResult), (
            f"{name}: call_tool returned {type(result).__name__}, not a "
            "CallToolResult -- InputRequiredResult is never expected here"
        )
        return result

    def _call_tool_error(
        self, server, name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        """Drive an error-path delivery assertion through the in-process
        client (design.md > "Drive delivery assertions through the public
        tool-call entry point"). The public entry point raises `ToolError`
        for a failing tool instead of delivering a result, so the error
        bound this measures is only observable through the client, which
        returns the assembled error `CallToolResult` for both a raised
        tool error and a framework argument-validation failure.
        """

        async def _call() -> mcp_types.CallToolResult:
            async with Client(server) as client:
                return await client.call_tool(name, arguments)

        return asyncio.run(_call())

    def _list_tools(self, server) -> dict[str, mcp_types.Tool]:
        return {tool.name: tool for tool in asyncio.run(server.list_tools())}

    def _serialize_delivery(self, delivered: mcp_types.CallToolResult) -> str:
        """Independent oracle for the wire-length measurement.

        Reaches the same wire-facing settings the SDK's own send path
        documents (`by_alias=True`, `exclude_none=True` --
        mcp/shared/session.py `_send_response`'s
        `response.model_dump(by_alias=True, mode="json",
        exclude_none=True)` and mcp/server/stdio.py's
        `session_message.message.model_dump_json(by_alias=True,
        exclude_none=True)`) through `model_dump()` + `json.dumps()` --
        a different pydantic call and a different JSON encoder (Python's
        `json` module, not pydantic-core's) than production's
        `model_dump_json()` -- so this oracle and the production
        measurement never derive from the same call.
        """
        return json.dumps(
            delivered.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _spy_builder_factory(self, real_builder, built_results):
        def spy_builder(*args, **kwargs):
            result = real_builder(*args, **kwargs)
            built_results.append(result)
            return result

        return spy_builder

    def _assert_exact_message_payload_shape(
        self,
        payload: dict[str, object],
        *,
        include_body: bool,
        expect_response_truncated: bool = False,
    ) -> None:
        serialized = json.loads(json.dumps(payload))
        expected_keys = {
            "id",
            "subject",
            "sender_name",
            "sender_email",
            "direction",
            "sent_at",
            "received_at",
            "message_id",
            "in_reply_to",
            "references",
            "pst_source",
            "folder_path",
            "to",
            "cc",
            "bcc",
            "attachments",
            "attachment_names",
            "recipients_total",
            "recipients_truncated",
            "attachments_total",
            "attachments_truncated",
        }
        if include_body:
            expected_keys |= {
                "rendered_body_text",
                "body_truncated",
                "body_total_chars",
            }
        if expect_response_truncated:
            # Only the whole-tool `mail_show` result carries this key; a
            # message embedded in `mail_thread`'s `messages` list does not
            # (the flag is top-level on that tool's own payload instead).
            expected_keys.add("response_truncated")
        if "truncated_fields" in serialized:
            expected_keys.add("truncated_fields")
        self.assertEqual(set(serialized), expected_keys)
        for forbidden in (
            "body_text",
            "raw_headers",
            "logical_locator",
            "source_input_mode",
            "source_requested_from",
        ):
            self.assertNotIn(forbidden, serialized)
        for role in ("to", "cc", "bcc"):
            for recipient in serialized[role]:
                self.assertEqual(set(recipient), {"name", "email"})
        for attachment in serialized["attachments"]:
            self.assertEqual(
                set(attachment),
                {
                    "id",
                    "attachment_name",
                    "content_hash",
                    "size_bytes",
                    "mime_type",
                    "storage_path",
                },
            )

    def _create_thread_fixture_db(
        self,
        root: Path,
        filename: str,
        count: int,
        *,
        subject_prefix: str = "Thread message",
        message_id_prefix: str = "thread-msg",
        body_text: Callable[[int], str] | None = None,
    ) -> Path:
        """Persist *count* messages chained by ``in_reply_to`` into one
        thread, ascending ``sent_at``, so `thread_key` assignment (storage.py
        `_assign_thread_key_for_new_message`) links them into a single
        component ordered 1..count by insertion (and therefore by db id).

        *subject_prefix*, *message_id_prefix*, and *body_text* default to
        this fixture's original shape; *body_text*, when given, is called
        with the zero-based message index and overrides the default
        ``f"body {i + 1}"``. `_create_oversized_thread_fixture_db` reuses
        this exact chaining loop rather than duplicating it, varying only
        these three.
        """
        database_path = initialize_database(root / filename)
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(count):
            raw: dict[str, object] = {
                "subject": f"{subject_prefix} {i + 1}",
                "sender": {"email": "alice@example.com"},
                "to": [{"email": "bob@example.com"}],
                "sent_at": (base + timedelta(minutes=i)).isoformat(),
                "body_text": body_text(i) if body_text is not None else f"body {i + 1}",
                "message_id": f"<{message_id_prefix}-{i}@example.com>",
            }
            if i > 0:
                raw["in_reply_to"] = f"<{message_id_prefix}-{i - 1}@example.com>"
            fixtures.append(raw)
        self._persist_messages(database_path, fixtures)
        return database_path

    def _persist_messages(
        self,
        database_path: Path,
        fixtures: list[dict[str, object]],
    ) -> None:
        source = PstSource(
            path=Path("C:/mail/archive.pst"),
            input_mode="file",
            requested_from=Path("C:/mail/archive.pst"),
        )
        connection = connect_database(database_path)
        try:
            # Nothing is self until declared (#268); these fixtures write
            # the operator as this address.
            add_self_address(connection, "sean3808@mailattic.test")
            for index, raw in enumerate(fixtures):
                message = build_normalized_message(
                    raw,
                    source=source,
                    folder_path="Inbox",
                    logical_locator=f"Inbox/{index + 1}",
                )
                storage_persist_message(connection, message)
            connection.commit()
        finally:
            connection.close()

    def _create_boundary_fixture_db(self, root: Path) -> Path:
        database_path = initialize_database(root / "boundary.db")
        self._persist_messages(
            database_path,
            [
                {
                    "subject": "Boundary early",
                    "sender": {"email": "boundary@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-21T00:30:00+08:00",
                    "body_text": "daykeyword early body",
                    "message_id": "<early@example.com>",
                },
                {
                    "subject": "Boundary late",
                    "sender": {"email": "boundary@example.com"},
                    "to": [
                        {"name": f"Bob {index}", "email": f"bob{index}@example.com"}
                        for index in range(6)
                    ],
                    "cc": [{"name": "Carol", "email": "carol@example.com"}],
                    "bcc": [{"name": "Dana", "email": "dana@example.com"}],
                    "sent_at": "2026-07-21T23:59:00+08:00",
                    "body_text": "<p>orderkeyword fullbodytoken late body</p>",
                    "message_id": "<late@example.com>",
                    "in_reply_to": "<parent@example.com>",
                    "references": "<root@example.com> <parent@example.com>",
                    "attachments": [
                        {
                            "attachment_name": f"late-{index}.pdf",
                            "content_hash": f"late-hash-{index}",
                            "size_bytes": 100 + index,
                            "mime_type": "application/pdf",
                            "storage_path": f"C:/cas/late-{index}.pdf",
                        }
                        for index in range(6)
                    ],
                },
                {
                    "subject": "Boundary utc",
                    "sender": {"email": "boundary@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-20T17:00:00+00:00",
                    "body_text": "orderkeyword utc body",
                    "message_id": "<utc@example.com>",
                },
                {
                    "subject": "Boundary previous day",
                    "sender": {"email": "boundary@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-20T23:30:00+08:00",
                    "body_text": "daykeyword previous body",
                    "message_id": "<previous@example.com>",
                },
                {
                    "subject": "Boundary next day",
                    "sender": {"email": "boundary@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-22T00:30:00+08:00",
                    "body_text": "daykeyword next body",
                    "message_id": "<next@example.com>",
                },
                {
                    # Discriminating naive value: 2026-07-20T20:00:00 with NO offset.
                    # Interpreted as UTC (the documented policy) it is 04:00 Taipei on
                    # the 21st -> inside the [2026-07-21, 2026-07-21] Taipei query and
                    # asserted included below. If naive were wrongly read as Taipei it
                    # would be 2026-07-20 20:00 -> excluded, failing this test. So the
                    # inclusion assertion actually proves the naive->UTC interpretation.
                    "subject": "Boundary naive",
                    "sender": {"email": "boundary@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-20T20:00:00",
                    "body_text": "orderkeyword naive body",
                    "message_id": "<naive@example.com>",
                },
            ],
        )
        return database_path

    def _create_single_message_db(
        self, root: Path, filename: str, subject: str
    ) -> Path:
        database_path = initialize_database(root / filename)
        self._persist_messages(
            database_path,
            [
                {
                    "subject": subject,
                    "sender": {
                        "name": "Mailbox Sender",
                        "email": "mailbox@example.com",
                    },
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-21T09:00:00+08:00",
                    "body_text": "precedence body",
                    "message_id": f"<{filename}@example.com>",
                    "attachments": [
                        {
                            "attachment_name": f"{subject}.pdf",
                            "content_hash": f"hash-{subject}",
                            "size_bytes": 12,
                            "mime_type": "application/pdf",
                            "storage_path": f"C:/cas/{subject}.pdf",
                        }
                    ],
                }
            ],
        )
        return database_path

    def _create_cjk_fixture_db(self, root: Path) -> Path:
        database_path = initialize_database(root / "cjk.db")
        self._persist_messages(
            database_path,
            [
                {
                    "subject": "測試主旨",
                    "sender": {"name": "測試寄件者", "email": "cjk@example.com"},
                    "to": [{"name": "收件者", "email": "reader@example.com"}],
                    "sent_at": "2026-07-21T10:00:00+08:00",
                    "body_text": "測試內容" * 2000,
                    "message_id": "<cjk@example.com>",
                }
            ],
        )
        return database_path

    def _create_oversized_body_fixture_db(self, root: Path) -> Path:
        """One message whose stored body is far larger than any
        `body_max_chars` a caller may request (max 100000), so a request
        near that ceiling produces an unreduced `mail_show` result that
        exceeds the delivery budget -- exercising task 2.2's ladder.
        Plain ASCII with no whitespace runs so `render_text` leaves the
        length exactly 150000 (no HTML stripping, no `.strip()` bite).
        """
        database_path = initialize_database(root / "oversized-body.db")
        self._persist_messages(
            database_path,
            [
                {
                    "subject": "Oversized body",
                    "sender": {"email": "oversized@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-21T09:00:00+08:00",
                    "body_text": "A" * 150000,
                    "message_id": "<oversized-body@example.com>",
                }
            ],
        )
        return database_path

    def _create_oversized_thread_fixture_db(
        self, root: Path, filename: str, count: int, body_chars: int
    ) -> Path:
        """Same chaining shape as `_create_thread_fixture_db`, but each
        message carries a *body_chars*-character body, so `full_body=True`
        at a generous `limit` produces an unreduced `mail_thread` result
        that exceeds the delivery budget -- exercising task 2.3's ladder.
        """
        return self._create_thread_fixture_db(
            root,
            filename,
            count,
            subject_prefix="Oversized thread message",
            message_id_prefix="oversized-thread-msg",
            body_text=lambda i: "B" * body_chars,
        )

    def _create_contact_history_fixture_db(self, root: Path) -> Path:
        database_path = initialize_database(root / "contact-history.db")
        self._persist_messages(
            database_path,
            [
                {
                    "subject": "Inbound from Alice",
                    "sender": {"name": "Alice", "email": "alice@acme.com"},
                    "to": [{"email": "sean3808@mailattic.test"}],
                    "sent_at": "2026-07-01T09:00:00+08:00",
                    "body_text": "alice body",
                    "message_id": "<alice@acme.com>",
                    "attachments": [{"attachment_name": "alice-note.pdf"}],
                },
                {
                    "subject": "Outbound to Bob",
                    "sender": {"email": "sean3808@mailattic.test"},
                    "to": [{"name": "Bob", "email": "bob@acme.com"}],
                    "sent_at": "2026-07-02T09:00:00+08:00",
                    "body_text": "bob body",
                    "message_id": "<bob@acme.com>",
                    "attachments": [{"attachment_name": "bob-note.pdf"}],
                },
                {
                    "subject": "Outbound to Carol",
                    "sender": {"email": "sean3808@mailattic.test"},
                    "to": [{"name": "Carol", "email": "carol@acme.com"}],
                    "sent_at": "2026-07-03T09:00:00+08:00",
                    "body_text": "carol body",
                    "message_id": "<carol@acme.com>",
                },
                {
                    "subject": "Outbound to Mallory",
                    "sender": {"email": "sean3808@mailattic.test"},
                    "to": [{"name": "Mallory", "email": "mallory@acme.com.evil"}],
                    "sent_at": "2026-07-04T09:00:00+08:00",
                    "body_text": "mallory body",
                    "message_id": "<mallory@acme.com.evil>",
                },
            ],
        )
        return database_path

    def _create_attachment_fixture_db(self, root: Path) -> Path:
        database_path = initialize_database(root / "attachments.db")
        self._persist_messages(
            database_path,
            [
                {
                    "subject": "Attachment newest",
                    "sender": {"email": "sender@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-10T09:00:00+08:00",
                    "body_text": "attachment body 1",
                    "message_id": "<attachment-1@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report-newest.pdf",
                            "content_hash": "shared-hash",
                            "size_bytes": 11,
                            "mime_type": "application/pdf",
                            "storage_path": "C:/cas/report-newest.pdf",
                        }
                    ],
                },
                {
                    "subject": "Attachment middle",
                    "sender": {"email": "sender@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-09T09:00:00+08:00",
                    "body_text": "attachment body 2",
                    "message_id": "<attachment-2@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report-middle.pdf",
                            "content_hash": "shared-hash",
                            "size_bytes": 12,
                            "mime_type": "application/pdf",
                            "storage_path": "C:/cas/report-middle.pdf",
                        }
                    ],
                },
                {
                    "subject": "Attachment oldest",
                    "sender": {"email": "sender@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-08T09:00:00+08:00",
                    "body_text": "attachment body 3",
                    "message_id": "<attachment-3@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report-oldest.pdf",
                            "content_hash": "shared-hash",
                            "size_bytes": 13,
                            "mime_type": "application/pdf",
                            "storage_path": "C:/cas/report-oldest.pdf",
                        }
                    ],
                },
                {
                    "subject": "Attachment unique",
                    "sender": {"email": "sender@example.com"},
                    "to": [{"email": "reader@example.com"}],
                    "sent_at": "2026-07-07T09:00:00+08:00",
                    "body_text": "attachment body 4",
                    "message_id": "<attachment-4@example.com>",
                    "attachments": [
                        {
                            "attachment_name": "report-unique.pdf",
                            "content_hash": "unique-hash",
                            "size_bytes": 14,
                            "mime_type": "application/pdf",
                            "storage_path": "C:/cas/report-unique.pdf",
                        }
                    ],
                },
            ],
        )
        return database_path

    def _create_oversized_attachment_dedup_fixture_db(
        self, root: Path, filename: str, count: int
    ) -> Path:
        """*count* messages sharing one attachment `content_hash`, each with
        a long `message_id`, so a `dedupe="content-hash"` query collapses
        them into ONE entry whose `carrying_messages` collection alone --
        at its caller-requested `carrying_messages_limit` -- exceeds the
        delivery budget. Exercises task 2.4's nested-collection-first
        reduction with nothing to drop at the outer level (there is only
        one entry).
        """
        database_path = initialize_database(root / filename)
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(count):
            fixtures.append(
                {
                    "subject": f"Shared attachment {i}",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": (base + timedelta(minutes=i)).isoformat(),
                    "body_text": f"body {i}",
                    "message_id": (
                        f"<oversized-attachment-msg-{i}-" + "X" * 500 + "@example.com>"
                    ),
                    "attachments": [
                        {
                            "attachment_name": "shared.pdf",
                            "content_hash": "shared-hash",
                            "size_bytes": 100 + i,
                            "mime_type": "application/pdf",
                            "storage_path": f"C:/cas/shared-{i}.pdf",
                        }
                    ],
                }
            )
        self._persist_messages(database_path, fixtures)
        return database_path

    def _create_oversized_attachment_entries_fixture_db(
        self, root: Path, filename: str, count: int
    ) -> Path:
        """*count* messages each with its own distinct `content_hash` and a
        moderately long `message_id`, so a plain (non-deduped) query
        returns *count* distinct entries whose combined size exceeds the
        delivery budget. Exercises task 2.4's oldest-entry-drop reduction
        -- each entry's own `carrying_messages` collection is already at
        its floor (one message per entry), so the ladder has nothing left
        to shrink there and must drop entries instead.
        """
        database_path = initialize_database(root / filename)
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(count):
            fixtures.append(
                {
                    "subject": f"Distinct attachment {i}",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": (base + timedelta(minutes=i)).isoformat(),
                    "body_text": f"body {i}",
                    "message_id": (
                        f"<oversized-attachment-distinct-{i}-"
                        + "X" * 50
                        + "@example.com>"
                    ),
                    "attachments": [
                        {
                            "attachment_name": f"distinct-{i}.pdf",
                            "content_hash": f"distinct-hash-{i}",
                            "size_bytes": 100 + i,
                            "mime_type": "application/pdf",
                            "storage_path": f"C:/cas/distinct-{i}.pdf",
                        }
                    ],
                }
            )
        self._persist_messages(database_path, fixtures)
        return database_path

    def _create_oversized_contact_history_fixture_db(
        self, root: Path, filename: str, count: int
    ) -> Path:
        """*count* distinct inbound senders on one domain, each with a
        190-character display name, so a `contacts_limit` at its declared
        maximum (200) still exceeds the delivery budget. Exercises task
        2.5's contacts-list-first reduction. Plain ASCII display names so
        no presentation-cap bit interferes with the size calibration.
        """
        database_path = initialize_database(root / filename)
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(count):
            fixtures.append(
                {
                    "subject": f"Oversized contact history {i}",
                    "sender": {
                        "name": "N" * 190,
                        "email": f"contact{i}@oversized-domain.example",
                    },
                    "to": [{"email": "sean3808@mailattic.test"}],
                    "sent_at": (base + timedelta(minutes=i)).isoformat(),
                    "body_text": f"body {i}",
                    "message_id": (
                        f"<oversized-contact-history-{i}@oversized-domain.example>"
                    ),
                }
            )
        self._persist_messages(database_path, fixtures)
        return database_path

    def _create_small_contact_history_with_attachments_fixture_db(
        self, root: Path
    ) -> Path:
        """Three distinct contacts on one domain, one of whom (`contact0`)
        also sent nine further messages each carrying a roughly
        292-character attachment name -- realistic-sized data that stays
        comfortably under the real 120,000-character budget on its own.

        `mail_contact_history`'s two reduction phases cannot both be forced
        by real data at the real budget: measured directly, a full
        100-entry, 300-character-name attachments overview alone
        serializes to well under half the budget, so it can never by
        itself push a payload whose contacts list is already at its floor
        back over the top. Paired with a patched (lowered) budget in its
        test, this fixture is small enough that both the contacts list and
        the attachments overview genuinely have to shrink, in that order.
        """
        database_path = initialize_database(root / "small-contact-history.db")
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(3):
            fixtures.append(
                {
                    "subject": f"Small contact history {i}",
                    "sender": {
                        "name": f"Contact {i}",
                        "email": f"contact{i}@small-domain.example",
                    },
                    "to": [{"email": "sean3808@mailattic.test"}],
                    "sent_at": (base + timedelta(minutes=i)).isoformat(),
                    "body_text": f"body {i}",
                    "message_id": f"<small-contact-history-{i}@small-domain.example>",
                    "attachments": [
                        {"attachment_name": f"note-{i}-" + "A" * 280 + ".pdf"}
                    ],
                }
            )
        for j in range(3, 12):
            fixtures.append(
                {
                    "subject": f"Small contact history extra {j}",
                    "sender": {
                        "name": "Contact 0",
                        "email": "contact0@small-domain.example",
                    },
                    "to": [{"email": "sean3808@mailattic.test"}],
                    "sent_at": (base + timedelta(minutes=j)).isoformat(),
                    "body_text": f"body {j}",
                    "message_id": (
                        f"<small-contact-history-extra-{j}@small-domain.example>"
                    ),
                    "attachments": [
                        {"attachment_name": f"note-{j}-" + "A" * 280 + ".pdf"}
                    ],
                }
            )
        self._persist_messages(database_path, fixtures)
        return database_path

    def test_registered_tool_set_is_six_read_tools_plus_attachment_export(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

        self.assertEqual(
            set(self._list_tools(server)),
            {
                "mail_search",
                "mail_show",
                "mail_thread",
                "mail_contact_history",
                "mail_attachments",
                "mail_sync_status",
                "mail_attachment_export",
                "mail_draft_reply",
                "mail_send",
            },
        )

    def _read_sync_state_row(self, database_path: Path) -> tuple[object, ...] | None:
        connection = connect_database(database_path, create=False, read_only=True)
        try:
            row = connection.execute(
                "SELECT started_at, ended_at, outcome FROM sync_state "
                "WHERE sync_key = ?",
                [SYNC_STATE_KEY],
            ).fetchone()
        finally:
            connection.close()
        return tuple(row) if row is not None else None

    def test_mail_sync_status_starts_no_synchronization_and_leaves_state_unchanged(
        self,
    ) -> None:
        """spec: 'No write, send, or sync tool surface', scenario 'The sync
        status tool starts no synchronization'. Compares the sync_state
        row's own fields before and after the call rather than only
        checking for the absence of a raised exception, so a violation
        that silently mutated the row would actually be caught. Covers
        both a database carrying an in-progress run and a database with no
        sync_state row at all -- calling the tool must create no row.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            running_db = self._create_boundary_fixture_db(root)
            self._seed_sync_row(
                running_db,
                started_at=datetime(
                    2026, 7, 20, 9, 0, 0, tzinfo=timezone(timedelta(hours=8))
                ),
            )
            running_server = create_server(database_path=running_db)
            running_before = self._read_sync_state_row(running_db)
            self._call_tool_success(running_server, "mail_sync_status", {})
            running_after = self._read_sync_state_row(running_db)

            never_synced_db = initialize_database(root / "never-synced-state.db")
            never_synced_server = create_server(database_path=never_synced_db)
            never_synced_before = self._read_sync_state_row(never_synced_db)
            self._call_tool_success(never_synced_server, "mail_sync_status", {})
            never_synced_after = self._read_sync_state_row(never_synced_db)

        self.assertIsNotNone(running_before)
        self.assertEqual(running_before, running_after)
        self.assertIsNone(never_synced_before)
        self.assertIsNone(never_synced_after)

    def test_mail_sync_status_reports_never_synced_before_any_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "never-synced.db")
            server = create_server(database_path=database_path)
            result = self._call_mail_sync_status(server)

        self.assertEqual(
            set(result),
            {
                "last_sync_started_at",
                "last_sync_ended_at",
                "last_sync_status",
                "latest_message_at",
            },
        )
        self.assertEqual(
            result,
            {
                "last_sync_started_at": None,
                "last_sync_ended_at": None,
                "last_sync_status": "never_synced",
                "latest_message_at": None,
            },
        )

    def test_mail_sync_status_reports_running_outcome_with_null_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            started = datetime(
                2026, 7, 21, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))
            )
            self._seed_sync_row(database_path, started_at=started)

            server = create_server(database_path=database_path)
            result = self._call_mail_sync_status(server)

        self.assertEqual(result["last_sync_started_at"], started.isoformat())
        self.assertIsNone(result["last_sync_ended_at"])
        self.assertEqual(result["last_sync_status"], "running")
        self.assertIsNotNone(result["latest_message_at"])

    def test_mail_sync_status_reports_completed_outcome_with_both_timestamps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            started = datetime(
                2026, 7, 21, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))
            )
            ended = datetime(2026, 7, 21, 8, 5, 0, tzinfo=timezone(timedelta(hours=8)))
            self._seed_sync_row(
                database_path,
                started_at=started,
                ended_at=ended,
                outcome="completed",
            )

            server = create_server(database_path=database_path)
            result = self._call_mail_sync_status(server)

        self.assertEqual(result["last_sync_started_at"], started.isoformat())
        self.assertEqual(result["last_sync_ended_at"], ended.isoformat())
        self.assertEqual(result["last_sync_status"], "completed")
        self.assertIsNotNone(result["latest_message_at"])

    def test_mail_sync_status_reports_failed_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            started = datetime(
                2026, 7, 21, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))
            )
            ended = datetime(2026, 7, 21, 8, 5, 0, tzinfo=timezone(timedelta(hours=8)))
            self._seed_sync_row(
                database_path,
                started_at=started,
                ended_at=ended,
                outcome="failed",
            )

            server = create_server(database_path=database_path)
            result = self._call_mail_sync_status(server)

        self.assertEqual(result["last_sync_started_at"], started.isoformat())
        self.assertEqual(result["last_sync_ended_at"], ended.isoformat())
        self.assertEqual(result["last_sync_status"], "failed")
        self.assertIsNotNone(result["latest_message_at"])

    def test_mail_sync_status_missing_database_surfaces_environment_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_db = Path(temp_dir) / "missing-sync-status.db"
            server = create_server(database_path=missing_db)

            delivered = self._call_tool_error(server, "mail_sync_status", {})

        self.assertTrue(delivered.is_error)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertIn(str(missing_db), text)
        self.assertIn("Mail database was not found", text)

    def test_mail_sync_status_database_error_surfaces_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            oversized_detail = "S" * 160_000
            with patch.object(
                mcp_readonly_module,
                "get_sync_status",
                side_effect=sqlite3.OperationalError(oversized_detail),
            ) as patched_get_sync_status:
                delivered = self._call_tool_error(server, "mail_sync_status", {})
                patched_get_sync_status.assert_called()

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertIn(str(database_path), text)
        self.assertNotIn(oversized_detail, text)

    def test_mail_sync_status_over_budget_payload_is_refused_not_delivered(
        self,
    ) -> None:
        """The tool measures at the assembly boundary like every other tool.

        Its four fields look small by construction, but the timestamp columns
        are unbounded TEXT, so that is an assumption about the data rather
        than a guarantee. Removing the measurement check delivers the
        oversized payload instead of refusing it, turning this red.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            pathological = "9" * 200_000
            with patch.object(
                mcp_readonly_module,
                "get_sync_status",
                return_value={
                    "last_sync_started_at": "2026-08-04T10:00:00+08:00",
                    "last_sync_ended_at": "2026-08-04T10:05:00+08:00",
                    "last_sync_status": "completed",
                    "latest_message_at": pathological,
                },
            ):
                delivered = self._call_tool_error(server, "mail_sync_status", {})

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertIn("mail_sync_status cannot be delivered", text)
        self.assertNotIn(pathological, text)

    def test_all_tools_declare_titles_annotations_and_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

        tools = self._list_tools(server)
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertTrue(tool.title)
                self.assertTrue(tool.description)
                self.assertIsNotNone(tool.annotations)
                assert tool.annotations is not None
                # Issue #28, #189: write-accessor tools have readOnlyHint=False.
                self.assertEqual(
                    tool.annotations.read_only_hint,
                    name
                    not in {
                        "mail_attachment_export",
                        "mail_draft_reply",
                        "mail_send",
                    },
                )
                self.assertFalse(tool.annotations.destructive_hint)
                # Each mail_draft_reply call writes a new draft; mail_send
                # reaches external recipients.
                self.assertEqual(
                    tool.annotations.idempotent_hint,
                    name not in {"mail_draft_reply", "mail_send"},
                )
                self.assertEqual(tool.annotations.open_world_hint, name == "mail_send")

        overlapping = {
            "mail_search": ("mail_show", "mail_thread"),
            "mail_show": ("mail_search", "mail_thread"),
            "mail_thread": ("mail_search", "mail_show"),
        }
        for name, other_names in overlapping.items():
            with self.subTest(tool=name):
                description = tools[name].description or ""
                self.assertTrue(any(other in description for other in other_names))

    def test_search_uses_id_key_and_orders_by_date_without_a_keyword(self) -> None:
        """Was `..._returns_newest_first_and_uses_id_key`, which pinned
        newest-first ordering for a KEYWORD search too. Issue #177 replaced
        that with relevance ordering when a keyword is given: under recency
        the message a quoted price originated in is the last one returned,
        because every later reply repeats the terms in its quoted tail. The
        superseded behaviour is described in the frozen
        `openspec/specs/cowork-mcp-bridge/spec.md`; the trade-off is recorded
        in docs/decisions/D-20260826-1.

        The date ordering asserted here is the no-keyword path, which #177
        left alone. Relevance ordering itself is asserted in
        tests/test_search_relevance.py against a fixture built for it -- not
        here, where pinning bm25's output on three incidental fixture bodies
        would assert an implementation detail rather than a behaviour.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            keyword_results = self._call_mail_search(
                server, keyword="orderkeyword", limit=10
            )
            dated_results = self._call_mail_search(
                server, sender="boundary@example.com", limit=10
            )

        self.assertEqual(
            [result["subject"] for result in dated_results],
            [
                "Boundary next day",
                "Boundary late",
                "Boundary naive",
                "Boundary utc",
                "Boundary early",
                "Boundary previous day",
            ],
        )
        self.assertEqual(
            sorted(str(result["subject"]) for result in keyword_results),
            ["Boundary late", "Boundary naive", "Boundary utc"],
        )
        for results in (keyword_results, dated_results):
            self.assertTrue(all("id" in result for result in results))
            self.assertTrue(all("db_id" not in result for result in results))
            self.assertTrue(
                all("rendered_body_text" not in result for result in results)
            )

    def test_mail_search_recipient_domain_excludes_overmatch_via_search_messages(
        self,
    ) -> None:
        """anchor-recipient-domain-filter: the `mail_search` tool's
        `recipient_domain` filter inherits the boundary fix through
        `search_messages`, with no MCP-layer edit. Fails if the recipient
        condition reverts to the unanchored substring pattern."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "mcp_domain_boundary.db"
            )
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Overmatch only recipient",
                        "sender": {"email": "someone1@other.com"},
                        "to": [{"email": "alice@acme.com.evil"}],
                        "sent_at": "2026-01-01T10:00:00",
                        "body_text": "body",
                        "message_id": "<mcp-overmatch@test>",
                    },
                    {
                        "subject": "Packed blob genuine recipient",
                        "sender": {"email": "someone2@other.com"},
                        "to": [{"email": "'sally@acme.com'; '某人'; 'other@ext.com'"}],
                        "sent_at": "2026-01-02T10:00:00",
                        "body_text": "body",
                        "message_id": "<mcp-packed@test>",
                    },
                ],
            )
            server = create_server(database_path=database_path)
            results = self._call_mail_search(
                server, recipient_domain="acme.com", limit=10
            )

        subjects = {result["subject"] for result in results}
        self.assertNotIn("Overmatch only recipient", subjects)
        self.assertIn("Packed blob genuine recipient", subjects)
        self.assertEqual(len(results), 1)

    def test_full_body_true_adds_documented_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            results = self._call_mail_search(
                server,
                keyword="fullbodytoken",
                limit=10,
                full_body=True,
            )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["subject"], "Boundary late")
        self.assertEqual(
            result["rendered_body_text"], "orderkeyword fullbodytoken late body"
        )
        self.assertEqual(result["message_id"], "<late@example.com>")
        self.assertEqual(result["dedup_key"], "message-id:<late@example.com>")
        self.assertEqual(result["in_reply_to"], "<parent@example.com>")
        self.assertEqual(
            result["references"], "<root@example.com> <parent@example.com>"
        )
        self.assertEqual(result["recipients_total"], {"to": 6, "cc": 1, "bcc": 1})
        self.assertTrue(result["recipients_truncated"])
        self.assertEqual(result["attachments_total"], 6)
        self.assertTrue(result["attachments_truncated"])

    def test_mail_search_allowlists_differ_by_mode_and_drop_logical_locator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            summary_rows = self._call_mail_search(
                server, keyword="fullbodytoken", limit=10, full_body=False
            )
            full_rows = self._call_mail_search(
                server, keyword="fullbodytoken", limit=10, full_body=True
            )

        summary_allowed = {
            "id",
            "subject",
            "sender_name",
            "sender_email",
            "direction",
            "sent_at",
            "received_at",
            "pst_source",
            "folder_path",
            "attachments",
            "attachment_names",
            "snippet",
            # issue #177 match evidence -- always present, both null without
            # a keyword.
            "match_field",
            "match_in",
            "attachments_total",
            "attachments_truncated",
            "truncated_fields",
        }
        full_allowed = summary_allowed | {
            "rendered_body_text",
            "message_id",
            "dedup_key",
            "in_reply_to",
            "references",
            "to",
            "cc",
            "bcc",
            "body_truncated",
            "body_total_chars",
            "recipients_total",
            "recipients_truncated",
        }

        self.assertEqual(len(summary_rows), 1)
        self.assertEqual(len(full_rows), 1)
        summary_row = summary_rows[0]
        full_row = full_rows[0]
        self.assertTrue(set(summary_row).issubset(summary_allowed))
        self.assertTrue(set(full_row).issubset(full_allowed))
        self.assertNotIn("logical_locator", summary_row)
        self.assertNotIn("logical_locator", full_row)
        self.assertNotIn("to", summary_row)
        self.assertNotIn("cc", summary_row)
        self.assertNotIn("bcc", summary_row)
        self.assertNotIn("recipients_total", summary_row)
        self.assertNotIn("recipients_truncated", summary_row)
        self.assertEqual(full_row["recipients_total"], {"to": 6, "cc": 1, "bcc": 1})
        self.assertTrue(full_row["recipients_truncated"])

    def test_mail_search_full_body_applies_body_max_chars(self) -> None:
        # `body_max_chars` is validated to the 1000..100000 range everywhere
        # in this module, so the fixture body must exceed 1000 characters for
        # a valid (in-range) budget to actually truncate it -- unlike the
        # short "Boundary late" fixture body, which no valid budget can cut.
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "longbody.db")
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Long body message",
                        "sender": {"email": "mailbox@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "orderkeyword " + ("x" * 2000),
                        "message_id": "<longbody@example.com>",
                    }
                ],
            )
            server = create_server(database_path=database_path)
            results = self._call_mail_search(
                server,
                keyword="orderkeyword",
                limit=10,
                full_body=True,
                body_max_chars=1000,
            )

        self.assertEqual(len(results[0]["rendered_body_text"]), 1000)
        self.assertTrue(results[0]["body_truncated"])
        self.assertGreater(results[0]["body_total_chars"], 1000)

    def test_mail_search_full_body_renders_pre_rendered_text_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "single-render.db")
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Single render",
                        "sender": {"email": "boundary@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T12:00:00+08:00",
                        "body_text": "singlerender Visible &lt;foo&gt; text",
                        "message_id": "<single-render@example.com>",
                    }
                ],
            )
            server = create_server(database_path=database_path)
            results = self._call_mail_search(
                server,
                keyword="singlerender",
                limit=10,
                full_body=True,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["rendered_body_text"],
            "singlerender Visible <foo> text",
        )
        self.assertEqual(
            results[0]["body_total_chars"],
            len("singlerender Visible <foo> text"),
        )

    def test_invalid_keyword_returns_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_search(server, keyword="PEN (", limit=10)

        message = str(ctx.exception)
        self.assertIn("Invalid FTS keyword query: PEN (", message)
        self.assertIn("uppercase AND / OR / NOT", message)
        self.assertIn("double-quoted phrases", message)

    def test_database_path_precedence_and_missing_db_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit_db = self._create_single_message_db(
                root, "explicit.db", "explicit-db"
            )
            env_db = self._create_single_message_db(root, "env.db", "env-db")
            default_db = self._create_single_message_db(
                root, "default.db", "default-db"
            )
            missing_db = root / "missing.db"

            with patch("mailattic.storage.DEFAULT_DB_NAME", str(default_db)):
                with patch.dict(os.environ, {ENV_VAR_DB: str(env_db)}, clear=False):
                    explicit_server = create_server(database_path=explicit_db)
                    env_server = create_server()
                with patch.dict(os.environ, {ENV_VAR_DB: "   "}, clear=False):
                    default_server = create_server()
                    missing_default_server = create_server(database_path=missing_db)

            explicit_result = self._call_mail_search(
                explicit_server, sender="mailbox@example.com", limit=10
            )
            env_result = self._call_mail_search(
                env_server, sender="mailbox@example.com", limit=10
            )
            default_result = self._call_mail_search(
                default_server, sender="mailbox@example.com", limit=10
            )

            self.assertEqual(
                [row["subject"] for row in explicit_result], ["explicit-db"]
            )
            self.assertEqual([row["subject"] for row in env_result], ["env-db"])
            self.assertEqual([row["subject"] for row in default_result], ["default-db"])

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_search(
                    missing_default_server, sender="mailbox@example.com", limit=10
                )
            self.assertFalse(missing_db.exists())

        message = str(ctx.exception)
        self.assertIn(str(missing_db), message)
        self.assertNotIn("mail_show", message)
        self.assertNotIn("mail_thread", message)

    def test_all_five_tools_honor_constructor_database_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit_db = self._create_single_message_db(
                root, "explicit.db", "explicit-db"
            )
            env_db = self._create_single_message_db(root, "env.db", "env-db")
            with patch.dict(os.environ, {ENV_VAR_DB: str(env_db)}, clear=False):
                server = create_server(database_path=explicit_db)

            search_result = self._call_mail_search(
                server, sender="mailbox@example.com", limit=10
            )
            show_result = self._call_mail_show(server, message_id=1)
            thread_result = self._call_mail_thread(server, identifier=1)
            history_result = self._call_mail_contact_history(
                server, contact="mailbox@example.com"
            )
            attachments_result = self._call_mail_attachments(
                server, name_contains="explicit-db"
            )

        self.assertEqual([row["subject"] for row in search_result], ["explicit-db"])
        self.assertEqual(show_result["subject"], "explicit-db")
        self.assertEqual(
            [row["subject"] for row in thread_result["messages"]], ["explicit-db"]
        )
        self.assertEqual(history_result["query"], "mailbox@example.com")
        self.assertEqual(history_result["total_messages"], 1)
        self.assertEqual(
            [row["subject"] for row in attachments_result["attachments"]],
            ["explicit-db"],
        )

    def test_read_only_connection_rejects_write_and_leaves_db_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            connection = connect_database(database_path, create=False, read_only=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(
                        "UPDATE messages SET subject = 'tampered' WHERE subject = ?",
                        ["Boundary late"],
                    )
            finally:
                connection.close()

            check_connection = connect_database(database_path, create=False)
            try:
                subject_row = check_connection.execute(
                    "SELECT subject FROM messages WHERE message_id = ?",
                    ["<late@example.com>"],
                ).fetchone()
            finally:
                check_connection.close()

        self.assertIsNotNone(subject_row)
        self.assertEqual(subject_row["subject"], "Boundary late")

    def test_empty_filter_and_invalid_limit_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "At least one mail_search filter"):
                self._call_mail_search(server)
            with self.assertRaisesRegex(ToolError, "Invalid limit: 0"):
                self._call_mail_search(server, sender="boundary@example.com", limit=0)
            with self.assertRaisesRegex(ToolError, "Invalid limit: 101"):
                self._call_mail_search(server, sender="boundary@example.com", limit=101)

    def test_strict_scalar_contract_rejects_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as show_ctx:
                self._call_mail_show(server, message_id=True)
            with self.assertRaises(ToolError) as limit_ctx:
                self._call_mail_search(server, sender="boundary@example.com", limit="1")
            with self.assertRaises(ToolError) as body_ctx:
                self._call_mail_search(
                    server, sender="boundary@example.com", full_body="yes"
                )

        self.assertIn("message_id", str(show_ctx.exception))
        self.assertIn("limit", str(limit_ctx.exception))
        self.assertIn("full_body", str(body_ctx.exception))

    def test_taipei_day_boundaries_include_offset_aware_and_naive_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            results = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-07-21",
                to_date="2026-07-21",
                limit=20,
            )

        self.assertEqual(
            {result["subject"] for result in results},
            {
                "Boundary early",
                "Boundary late",
                "Boundary utc",
                "Boundary naive",
            },
        )

    def test_naive_stored_send_time_discriminating_pair(self) -> None:
        """Task 4.2: prove the naive-stored-send-time reading is UTC, not
        Taipei, with a pair that a mistaken Taipei reading would reverse.

        "Boundary naive" is stored as ``2026-07-20T20:00:00`` with no
        offset. Read as UTC it is 2026-07-21 04:00 Taipei (included in the
        07-21 window, excluded from the 07-20 window). A same-day case
        would pass under either reading and is not acceptable evidence
        (design.md > "Interpret a naive stored send time as UTC and
        record it as a known limitation").
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            included = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-07-21",
                to_date="2026-07-21",
                limit=20,
            )
            excluded = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-07-20",
                to_date="2026-07-20",
                limit=20,
            )

        self.assertIn("Boundary naive", {result["subject"] for result in included})
        self.assertNotIn("Boundary naive", {result["subject"] for result in excluded})

    def test_non_canonical_date_forms_still_accepted_and_equivalent(self) -> None:
        """Task 4.1: `mail_search` still accepts `20260721` and
        `2026-W30-2` -- it always has, since it parses with
        `date.fromisoformat` -- and each still resolves to the Taipei day
        `2026-07-21`, matching the canonical spelling. Turning either into
        a tool error would be a narrowing this change forbids.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            canonical = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-07-21",
                to_date="2026-07-21",
                limit=20,
            )
            basic = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="20260721",
                to_date="20260721",
                limit=20,
            )
            iso_week = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-W30-2",
                to_date="2026-W30-2",
                limit=20,
            )

        canonical_subjects = {result["subject"] for result in canonical}
        self.assertEqual(canonical_subjects, {result["subject"] for result in basic})
        self.assertEqual(canonical_subjects, {result["subject"] for result in iso_week})

    def test_padded_date_value_is_accepted_and_equivalent(self) -> None:
        """`mail_search` has always stripped before parsing (unlike
        `search` / `attachments` / `contact-history`, none of which do), so
        a whitespace-padded value must still be accepted and resolve to the
        same Taipei day as the canonical spelling."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            canonical = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="2026-07-21",
                to_date="2026-07-21",
                limit=20,
            )
            padded = self._call_mail_search(
                server,
                sender="boundary@example.com",
                from_date="  2026-07-21  ",
                to_date="  2026-07-21  ",
                limit=20,
            )

        self.assertEqual(
            {result["subject"] for result in canonical},
            {result["subject"] for result in padded},
        )

    def test_malformed_date_raises_tool_error_naming_value_and_parameter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_search(
                    server, sender="boundary@example.com", from_date="2026-201"
                )
        # Task 5.1's failure-mode matrix requires the diagnostic to name
        # both the offending value and the parameter it arrived through.
        message = str(ctx.exception)
        self.assertIn("2026-201", message)
        self.assertIn("from_date", message)

    def test_single_digit_and_full_timestamp_forms_still_rejected(self) -> None:
        """`mail_search`'s `date.fromisoformat` parser has never accepted a
        single-digit-month/day form or a full ISO-8601 timestamp -- both
        must still raise a ``ToolError``, unchanged by this change's
        render-time coercion revert.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError):
                self._call_mail_search(
                    server, sender="boundary@example.com", from_date="2026-7-1"
                )
            with self.assertRaises(ToolError):
                self._call_mail_search(
                    server,
                    sender="boundary@example.com",
                    from_date="2026-07-21T12:00:00",
                )

    def test_blank_date_value_is_unsupplied_not_a_date_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "At least one mail_search filter"):
                self._call_mail_search(server, from_date="   ", to_date="")

    # -- mail_show (tasks 5.1, 5.2) -----------------------------------------

    def test_mail_show_known_id_returns_allowlisted_payload_with_rendered_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            result = self._call_mail_show(server, message_id=1)

        self.assertEqual(result["subject"], "Boundary early")
        self.assertEqual(result["rendered_body_text"], "daykeyword early body")
        self.assertFalse(result["body_truncated"])
        self.assertEqual(result["body_total_chars"], len("daykeyword early body"))
        for forbidden in (
            "body_text",
            "raw_headers",
            "logical_locator",
            "source_input_mode",
            "source_requested_from",
        ):
            self.assertNotIn(forbidden, result)

    def test_registered_mail_show_and_mail_thread_preserve_exact_allowlists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            show_payload = json.loads(
                json.dumps(self._call_mail_show(server, message_id=2))
            )
            thread_payload = json.loads(
                json.dumps(
                    self._call_mail_thread(
                        server,
                        identifier=2,
                        limit=10,
                        full_body=True,
                    )
                )
            )

        self._assert_exact_message_payload_shape(
            show_payload, include_body=True, expect_response_truncated=True
        )
        self.assertEqual(
            set(thread_payload),
            {
                "messages",
                "total_messages",
                "returned_messages",
                "omitted_earlier_count",
                "response_truncated",
            },
        )
        self.assertEqual(
            thread_payload["returned_messages"],
            len(thread_payload["messages"]),
        )
        for message in thread_payload["messages"]:
            self._assert_exact_message_payload_shape(message, include_body=True)

    def test_mail_show_unknown_id_names_mail_search(self) -> None:
        """Task 3.3: a stored id that matches nothing is the same not-found
        error naming `mail_search` whether sent as an integer or as its
        ASCII decimal string form -- both are canonical class-1 forms of
        the same id.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_search"):
                self._call_mail_show(server, message_id=999999)
            with self.assertRaisesRegex(ToolError, "mail_search"):
                self._call_mail_show(server, message_id="999999")

    def test_mail_show_string_identifier_reaches_body_and_redirects_to_mail_thread(
        self,
    ) -> None:
        """Task 5.2: pins that `message_id` is declared int | str so a
        Message-ID string actually reaches the tool body. If the parameter
        were narrowed back to `int`, the MCP schema validator would reject
        the value before the body ran and this assertion (which requires the
        word "mail_thread" in the raised message) would fail.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_thread"):
                self._call_mail_show(server, message_id="<early@example.com>")

    def test_mail_show_bare_message_id_without_brackets_still_redirects(self) -> None:
        """Case C / class 2 (spec's classification example, bare
        Message-ID form): class 2 is defined by the presence of `@`, not by
        angle brackets -- a bare-address-form Message-ID must still redirect
        to `mail_thread` rather than falling into the class-3 error.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_thread"):
                self._call_mail_show(server, message_id="early@example.com")

    def test_mail_show_string_stored_id_matches_integer_form_field_for_field(
        self,
    ) -> None:
        """Case A (task 3.1): the ASCII decimal string form of a stored id
        returns the identical object, field for field, as the integer call
        -- not merely "no error".
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            integer_result = self._call_mail_show(server, message_id=1)
            string_result = self._call_mail_show(server, message_id="1")

        self.assertEqual(integer_result, string_result)

    def test_mail_show_sqlite_max_boundary_string_is_canonical_stored_id(
        self,
    ) -> None:
        """Case G (spec's classification example): the SQLite max-integer
        string is a valid class-1 form -- it is accepted, converted, and
        reaches the ordinary not-found error naming `mail_search`, not the
        class-3 identifier error.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_search"):
                self._call_mail_show(server, message_id="9223372036854775807")

    def test_mail_show_class_three_identifiers_rejected_without_redirect(
        self,
    ) -> None:
        """Case B (task 3.2): every class-3 row of the delta spec's
        classification example must be rejected with the class-3 identifier
        error, never the `mail_thread` redirect, and must never reach the
        query layer. Table-driven with one subTest per value so a
        regression names the offending value rather than "a" value failing.
        """
        class_three_values = {
            "arabic-indic-digits": "٦٠٦٤١",
            "underscore-separated": "1_0",
            "trailing-space": "7 ",
            "negative": "-5",
            "leading-zero": "007",
            "sqlite-max-plus-one": "9223372036854775808",
            "twenty-digits": "12345678901234567890",
            "empty-string": "",
            "non-numeric-word": "abc",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            for label, value in class_three_values.items():
                with self.subTest(case=label, value=value):
                    with (
                        patch.object(
                            mcp_readonly_module, "get_message"
                        ) as spied_get_message,
                        self.assertRaises(ToolError) as ctx,
                    ):
                        self._call_mail_show(server, message_id=value)
                    # tasks.md 3.5, database-never-reached class: the whole
                    # point is that identifier validation rejects before any
                    # read, so the accessor seam must stay untouched.
                    spied_get_message.assert_not_called()
                    message = str(ctx.exception)
                    self.assertIn("is not a valid stored id", message)
                    self.assertIn(
                        "Provide an integer, or its plain ASCII decimal string "
                        "form within the stored-id range",
                        message,
                    )
                    self.assertNotIn("mail_thread", message)
                    spied_get_message.assert_not_called()

    def test_mail_show_combined_invalid_identifier_and_sizing_reports_identifier_first(
        self,
    ) -> None:
        """Case F (task 2.2): the identifier classification runs ahead of
        `_validate_message_sizing_bounds` -- a call carrying both an invalid
        identifier and an out-of-range `body_max_chars` reports the
        identifier problem, not the sizing bound.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_show(server, message_id="abc", body_max_chars=999)

        message = str(ctx.exception)
        self.assertIn("is not a valid stored id", message)
        self.assertNotIn("body_max_chars", message)

    def test_is_canonical_stored_id_string_magnitude_boundary(self) -> None:
        """Case D (task 2.1a, helper level): the paired magnitude boundary
        directly on the predicate. The 4301-digit string is asserted here
        rather than through a tool call, because the framework's own
        `json.loads` pre-parse intercepts a string that long before the
        tool body ever runs -- a tool-call assertion there would be
        asserting the framework's behaviour, not ours.
        """
        self.assertTrue(
            mcp_server_module._is_canonical_stored_id_string("9223372036854775807")
        )
        self.assertFalse(
            mcp_server_module._is_canonical_stored_id_string("9223372036854775808")
        )
        self.assertFalse(mcp_server_module._is_canonical_stored_id_string("9" * 4301))

    # -- mail_thread (tasks 6.1, 6.2, 6.3, 6.4) ------------------------------

    def test_mail_thread_whole_thread_fits_within_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread3.db", 3
            )
            server = create_server(database_path=database_path)
            result = self._call_mail_thread(server, identifier=1, limit=10)

        self.assertEqual(
            [message["subject"] for message in result["messages"]],
            ["Thread message 1", "Thread message 2", "Thread message 3"],
        )
        self.assertEqual(result["total_messages"], 3)
        self.assertEqual(result["returned_messages"], 3)
        self.assertEqual(result["omitted_earlier_count"], 0)

    def test_mail_thread_truncates_from_old_end_reproducing_spec_example(
        self,
    ) -> None:
        """Reproduces the spec's "30-message thread requested with limit 5"
        example: messages 26-30 in ascending order, total 30, omitted 25.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread30.db", 30
            )
            server = create_server(database_path=database_path)
            result = self._call_mail_thread(server, identifier=1, limit=5)

        self.assertEqual(
            [message["subject"] for message in result["messages"]],
            [f"Thread message {n}" for n in range(26, 31)],
        )
        self.assertEqual(result["total_messages"], 30)
        self.assertEqual(result["returned_messages"], 5)
        self.assertEqual(result["omitted_earlier_count"], 25)

    def test_mail_thread_reports_true_total_beyond_the_cli_100_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread101.db", 101
            )
            server = create_server(database_path=database_path)
            result = self._call_mail_thread(server, identifier=1, limit=10)

        self.assertEqual(result["total_messages"], 101)
        self.assertEqual(result["returned_messages"], 10)
        self.assertEqual(result["omitted_earlier_count"], 91)

    def test_mail_thread_full_body_flag_controls_body_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread3.db", 3
            )
            server = create_server(database_path=database_path)
            without_body = self._call_mail_thread(server, identifier=1, limit=10)
            with_body = self._call_mail_thread(
                server, identifier=1, limit=10, full_body=True
            )

        for entry in without_body["messages"]:
            self.assertNotIn("rendered_body_text", entry)
            self.assertNotIn("body_truncated", entry)
            self.assertNotIn("body_total_chars", entry)
        for entry in with_body["messages"]:
            self.assertIn("rendered_body_text", entry)
            self.assertIn("body_truncated", entry)
            self.assertIn("body_total_chars", entry)

    def test_mail_thread_unknown_identifier_names_mail_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread1.db", 1
            )
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_search"):
                self._call_mail_thread(server, identifier=999999)

    # -- out-of-range identifier gate (change
    #    fix-thread-resolver-out-of-range-identifier, task 3.1) -------------

    _OTHER_REGISTERED_TOOL_NAMES = (
        "mail_search",
        "mail_show",
        "mail_contact_history",
        "mail_attachments",
        "mail_sync_status",
        "mail_attachment_export",
    )

    def _assert_classified_identifier_error(self, message: str) -> None:
        """Shared assertion for the ThreadIdentifierError mapping: states
        the accepted forms, names no other tool, and is not the
        `_unexpected_tool_error` wrapper text."""
        self.assertIn("signed 64-bit range", message)
        self.assertIn("RFC822 Message-ID", message)
        self.assertNotIn("Unexpected error", message)
        for other_tool in self._OTHER_REGISTERED_TOOL_NAMES:
            self.assertNotIn(other_tool, message)

    def test_mail_thread_over_bound_digit_string_reports_classified_error(
        self,
    ) -> None:
        """A 19-20 digit over-bound string: the framework's JSON pre-parse
        hands the tool body its integer (design.md's #54-derived caveat, MCP
        `mail_thread` bullet) before the resolver's int gate runs."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread1.db", 1
            )
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_thread(server, identifier="9223372036854775808")

        self._assert_classified_identifier_error(str(ctx.exception))

    def test_mail_thread_over_bound_int_reports_classified_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread1.db", 1
            )
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_thread(server, identifier=9223372036854775808)

        self._assert_classified_identifier_error(str(ctx.exception))

    def test_mail_thread_int_beyond_render_limit_reports_classified_error(
        self,
    ) -> None:
        """An int wider than CPython's int-to-str digit limit: only
        reachable on a direct in-process call (the JSON wire cannot express
        it), and must still be rejected by the dedicated exception rather
        than crashing on decimal rendering."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread1.db", 1
            )
            server = create_server(database_path=database_path)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_thread(server, identifier=10**4300)

        self._assert_classified_identifier_error(str(ctx.exception))

    def test_mail_thread_int64_max_boundary_still_reaches_not_found(self) -> None:
        """`9223372036854775807` (signed 64-bit max) is in range and must
        still reach the ordinary not-found error naming `mail_search`, not
        the classified identifier error."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread1.db", 1
            )
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "mail_search"):
                self._call_mail_thread(server, identifier=9223372036854775807)

    def test_mail_thread_registered_tool_runs_read_only_end_to_end(self) -> None:
        """Task 6.3: drives the read-only guarantee from the *registered*
        `mail_thread` tool entry point, not from the query functions
        directly -- a per-function test suite could stay green while the
        tool itself were wired to a writable path.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_thread_fixture_db(
                Path(temp_dir), "thread3.db", 3
            )
            server = create_server(database_path=database_path)

            real_connect_database = query_module.connect_database
            observed_read_only_flags: list[bool] = []

            def spy_connect_database(path, *, create=False, read_only=False):
                observed_read_only_flags.append(read_only)
                connection = real_connect_database(
                    path, create=create, read_only=read_only
                )
                if read_only:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute(
                            "UPDATE messages SET subject = 'tampered' WHERE id = 1"
                        )
                return connection

            with patch.object(
                query_module,
                "connect_database",
                side_effect=spy_connect_database,
            ):
                result = self._call_mail_thread(server, identifier=1, limit=10)

        self.assertEqual(result["total_messages"], 3)
        # The resolver opens one connection, and each of the 3 hydration
        # reads (get_message) opens its own -- every one of them must have
        # been opened read-only for the write-rejection assertions above to
        # have actually run.
        self.assertTrue(observed_read_only_flags)
        self.assertTrue(all(observed_read_only_flags))

    def test_delivery_builder_matches_framework_conversion_for_all_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary_db = self._create_boundary_fixture_db(root)
            attachment_db = self._create_attachment_fixture_db(root)
            server = create_server(database_path=boundary_db)
            attachment_server = create_server(database_path=attachment_db)
            cases = [
                (
                    "mail_search",
                    server,
                    {"sender": "boundary@example.com", "limit": 10},
                ),
                ("mail_show", server, {"message_id": 2}),
                (
                    "mail_thread",
                    server,
                    {"identifier": 2, "limit": 10, "full_body": True},
                ),
                ("mail_contact_history", server, {"contact": "boundary@example.com"}),
                (
                    "mail_attachments",
                    attachment_server,
                    {"name_contains": "report", "dedupe": "content-hash"},
                ),
                ("mail_sync_status", server, {}),
            ]

            for name, current_server, arguments in cases:
                with self.subTest(tool=name):
                    delivered = self._call_tool_success(current_server, name, arguments)
                    self.assertFalse(delivered.is_error)
                    self.assertIsNotNone(delivered.structured_content)
                    assert delivered.structured_content is not None
                    logical_payload = (
                        delivered.structured_content["result"]
                        if set(delivered.structured_content) == {"result"}
                        else delivered.structured_content
                    )
                    # The former equality assertion between our construction
                    # and a second copy of the framework conversion is GONE,
                    # and its removal is recorded rather than silent. On the
                    # 2.x generation the conversion returns a finished
                    # CallToolResult and build_call_tool_result returns it
                    # unchanged, so both sides of that comparison were the
                    # same object: it would have passed unconditionally,
                    # including whenever the delivery construction was wrong
                    # (spec > "The delivered response is measured at the
                    # assembly boundary"). A guard that cannot fail is not
                    # evidence, so it is deleted rather than kept as
                    # decoration. What replaces it below has two independent
                    # sides.
                    tool = current_server._tool_manager.get_tool(name)
                    assert tool is not None
                    self.assertIsNotNone(
                        tool.fn_metadata.output_schema,
                        msg=f"{name} lost its output schema",
                    )

                    # Shape pin -- the guard the branch removal depends on.
                    # build_call_tool_result deletes every branch for a shape
                    # the conversion cannot return, so a generation that
                    # reintroduces one must fail HERE rather than silently
                    # degrade delivery.
                    converted = tool.fn_metadata.convert_result(logical_payload)
                    self.assertIsInstance(
                        converted,
                        mcp_types.CallToolResult,
                        msg=(
                            f"{name}: convert_result returned "
                            f"{type(converted).__name__}, not the finished "
                            "CallToolResult mcp_delivery now delegates to"
                        ),
                    )

                    # Independent-oracle check, which keeps discriminating
                    # power the equality above lost: the delivered result is
                    # serialized by the production measurement and by an
                    # oracle reaching the same wire-facing settings through a
                    # different pydantic call and a different JSON encoder,
                    # so the two never derive from one call.
                    self.assertEqual(
                        measure_call_tool_result_chars(delivered),
                        len(self._serialize_delivery(delivered)),
                        msg=f"{name}: production measurement and oracle disagree",
                    )

    def test_cjk_delivery_measurement_uses_unescaped_json_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_cjk_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(server, "mail_show", {"message_id": 1})

        measured = measure_call_tool_result_chars(delivered)
        unescaped = self._serialize_delivery(delivered)
        # Same by_alias/exclude_none settings as the oracle above, so the
        # only variable between `escaped` and `unescaped` is the ASCII
        # escaping this assertion is actually about.
        escaped = json.dumps(
            delivered.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self.assertEqual(measured, len(unescaped))
        self.assertLess(measured, len(escaped))
        self.assertGreater(len(escaped), measured * 2)
        self.assertLess(measured, MCP_RESPONSE_BUDGET_CHARS)

    def test_registered_tools_do_not_expose_a_budget_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

        for name, tool in self._list_tools(server).items():
            with self.subTest(tool=name):
                property_names = set(tool.input_schema.get("properties", {}))
                self.assertFalse(
                    any(
                        "budget" in property_name.lower()
                        for property_name in property_names
                    )
                )

    def test_successful_delivery_is_the_tool_return_and_output_schema_survives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary_db = self._create_boundary_fixture_db(root)
            attachment_db = self._create_attachment_fixture_db(root)
            server = create_server(database_path=boundary_db)
            attachment_server = create_server(database_path=attachment_db)
            real_builder = mcp_server_module.build_call_tool_result
            cases = [
                (
                    "mail_search",
                    server,
                    {"sender": "boundary@example.com", "limit": 10},
                ),
                ("mail_show", server, {"message_id": 1}),
                ("mail_thread", server, {"identifier": 1}),
                ("mail_contact_history", server, {"contact": "boundary@example.com"}),
                ("mail_attachments", attachment_server, {"name_contains": "report"}),
            ]

            for name, current_server, arguments in cases:
                with self.subTest(tool=name):
                    built_results: list[mcp_types.CallToolResult] = []

                    spy = self._spy_builder_factory(real_builder, built_results)
                    # Two distinct name bindings reach the builder: the
                    # non-ladder tools call the one imported into mcp_server,
                    # the ladder-driven ones reach it through walk_ladder in
                    # mcp_delivery. Patch both with one shared recorder so the
                    # "built exactly once, and that object is what shipped"
                    # assertion holds whichever path a tool takes.
                    with (
                        patch.object(
                            mcp_server_module, "build_call_tool_result", side_effect=spy
                        ),
                        patch.object(
                            mcp_delivery_module,
                            "build_call_tool_result",
                            side_effect=spy,
                        ),
                    ):
                        delivered = self._call_tool_success(
                            current_server, name, arguments
                        )

                    # Exactly one build: these fixtures all fit the budget, so
                    # a ladder-driven tool stops at its first state. More than
                    # one here would mean an in-budget payload was needlessly
                    # re-measured.
                    self.assertEqual(len(built_results), 1)
                    self.assertIs(delivered, built_results[0])
                    self.assertFalse(delivered.is_error)
                    self.assertTrue(delivered.content)
                    self.assertIsNotNone(delivered.structured_content)
                    self.assertIsNotNone(
                        self._list_tools(current_server)[name].output_schema
                    )

    @pytest.mark.slow
    def test_budget_reduced_delivery_is_itself_within_budget(self) -> None:
        """The object that ships must be the object that passed the check.

        A ladder that measures one payload and then delivers a slightly
        different one -- for instance by stamping ``response_truncated``
        onto the winner after the walk -- would satisfy every truncation
        assertion while shipping more bytes than were ever measured. The
        delta is small, but the assembly-boundary rule is that the measured
        object IS the delivered object, so it is asserted on the delivered
        object directly rather than inferred from the ladder's bookkeeping.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            body_db = self._create_oversized_body_fixture_db(root)
            thread_db = self._create_oversized_thread_fixture_db(
                root,
                filename="budget-reduced-thread.db",
                count=10,
                body_chars=20000,
            )
            cases = [
                (
                    "mail_show",
                    create_server(database_path=body_db),
                    {"message_id": 1, "body_max_chars": 100000},
                ),
                (
                    "mail_thread",
                    create_server(database_path=thread_db),
                    {"identifier": 1, "limit": 10, "full_body": True},
                ),
            ]

            for name, server, arguments in cases:
                with self.subTest(tool=name):
                    delivered = self._call_tool_success(server, name, arguments)

                    self.assertFalse(delivered.is_error)
                    self.assertLessEqual(
                        measure_call_tool_result_chars(delivered),
                        MCP_RESPONSE_BUDGET_CHARS,
                    )
                    assert delivered.structured_content is not None
                    self.assertTrue(
                        delivered.structured_content["response_truncated"],
                        msg=f"{name}: fixture was meant to exceed the budget",
                    )

    def test_successful_results_keep_both_channels_and_errors_need_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            success_cases = [
                ("mail_search", {"sender": "boundary@example.com", "limit": 10}),
                ("mail_show", {"message_id": 1}),
                ("mail_thread", {"identifier": 1}),
                ("mail_contact_history", {"contact": "boundary@example.com"}),
                ("mail_attachments", {"name_contains": "late-0"}),
            ]

            for name, arguments in success_cases:
                with self.subTest(tool=name):
                    delivered = self._call_tool_success(server, name, arguments)
                    self.assertFalse(delivered.is_error)
                    self.assertTrue(delivered.content)
                    self.assertIsNotNone(delivered.structured_content)
                    if name == "mail_search":
                        assert delivered.structured_content is not None
                        self.assertIn("result", delivered.structured_content)
                        self.assertIsInstance(
                            delivered.structured_content["result"], list
                        )

            error_result = self._call_tool_error(server, "mail_search", {})

        self.assertTrue(error_result.is_error)
        self.assertTrue(error_result.content)
        self.assertIsNone(error_result.structured_content)

    # -- ladder driver (task 2.1) --------------------------------------

    def test_ladder_driver_stops_at_first_in_budget_state_without_calling_later_ones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
        tool = server._tool_manager.get_tool("mail_search")
        assert tool is not None

        called_with: list[int] = []

        def state(item_count: int) -> Callable[[], list[dict[str, object]]]:
            def make() -> list[dict[str, object]]:
                called_with.append(item_count)
                return [{"value": "x" * 30000} for _ in range(item_count)]

            return make

        # Calibrated (measured directly with build_call_tool_result /
        # measure_call_tool_result_chars): 5 items ~300k chars (over
        # budget), 2 items ~120k chars (over budget), 1 item ~60k chars
        # (fits comfortably under MCP_RESPONSE_BUDGET_CHARS).
        outcome = walk_ladder([state(5), state(2), state(1)], tool=tool)

        self.assertEqual(outcome.state_index, 2)
        self.assertTrue(outcome.reduced)
        self.assertLessEqual(
            measure_call_tool_result_chars(outcome.delivered),
            MCP_RESPONSE_BUDGET_CHARS,
        )
        # Only the states up to and including the winning one were ever
        # called -- no state after the winner was evaluated.
        self.assertEqual(called_with, [5, 2, 1])

    def test_ladder_driver_measures_in_budget_first_state_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
        tool = server._tool_manager.get_tool("mail_search")
        assert tool is not None

        called_with: list[int] = []
        later_state_called: list[bool] = []

        def first_state() -> list[dict[str, object]]:
            called_with.append(1)
            return [{"value": "small"}]

        def later_state() -> list[dict[str, object]]:
            later_state_called.append(True)
            return [{"value": "should never be called"}]

        outcome = walk_ladder([first_state, later_state], tool=tool)

        self.assertEqual(outcome.state_index, 0)
        self.assertFalse(outcome.reduced)
        self.assertEqual(called_with, [1])
        self.assertEqual(later_state_called, [])

    def test_ladder_driver_signals_exhaustion_distinctly_from_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
        tool = server._tool_manager.get_tool("mail_search")
        assert tool is not None

        oversized = lambda: [{"value": "x" * 30000} for _ in range(5)]  # noqa: E731

        with self.assertRaises(LadderExhausted) as ctx:
            walk_ladder([oversized, oversized], tool=tool)
        self.assertEqual(ctx.exception.state_count, 2)

    # -- response_truncated (task 3.1) -----------------------------------

    def test_flag_ladder_no_op_reduction_never_claims_a_reduction(self) -> None:
        # A no-op reduction step -- one whose bound was already at or above
        # the actual size of what it bounds -- produces content byte-
        # identical to the unreduced state's. Content-based flagging must
        # never stamp such a state `response_truncated: True` just because
        # it sits at a later ladder index (positional flagging did, and
        # because `true` serializes one character shorter than `false`,
        # that mis-flagged state could even slip under budget claiming a
        # reduction that never happened).
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
        tool = server._tool_manager.get_tool("mail_show")
        assert tool is not None

        content_a = {"value": "x" * 50}  # shared by states 0 and 1 (no-op)
        content_b = {"value": "short"}  # state 2: a genuine reduction

        states: list[Callable[[], dict[str, object]]] = [
            lambda: dict(content_a),
            lambda: dict(content_a),  # no-op: identical content to state 0
            lambda: dict(content_b),  # genuinely smaller content
        ]
        flagged = mcp_server_module._flag_ladder_states(states)

        # Pin the budget to exactly one character below what state 0's own
        # delivered result measures -- the width of "false" vs "true" --
        # without needing to construct a byte-exact end-to-end budget
        # boundary against the real 120,000-char budget.
        baseline_delivered = mcp_delivery_module.build_call_tool_result(
            {**content_a, "response_truncated": False}, tool=tool
        )
        unreduced_size = measure_call_tool_result_chars(baseline_delivered)

        with patch.object(
            mcp_delivery_module, "MCP_RESPONSE_BUDGET_CHARS", unreduced_size - 1
        ):
            outcome = walk_ladder(flagged, tool=tool)

        self.assertEqual(
            outcome.state_index,
            2,
            "a no-op reduction step (identical content to the unreduced "
            "state) must never be selected as if it were a genuine "
            "reduction",
        )
        assert outcome.delivered.structured_content is not None
        self.assertTrue(outcome.delivered.structured_content["response_truncated"])
        self.assertEqual(
            outcome.delivered.structured_content["value"], content_b["value"]
        )

    def test_response_truncated_present_and_false_on_in_budget_degradable_tools(
        self,
    ) -> None:
        # Task-file constraint: `_call_mail_*` bypasses the envelope and
        # MUST NOT be used for a budget-related assertion, so this drives
        # every case through `_call_tool_success` (the public tool-call
        # entry point).
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            attachment_db = self._create_attachment_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            attachment_server = create_server(database_path=attachment_db)

            cases = [
                ("mail_show", server, {"message_id": 1}),
                ("mail_thread", server, {"identifier": 1}),
                (
                    "mail_contact_history",
                    server,
                    {"contact": "boundary@example.com"},
                ),
                (
                    "mail_attachments",
                    attachment_server,
                    {"name_contains": "report"},
                ),
            ]

            for name, current_server, arguments in cases:
                with self.subTest(tool=name):
                    delivered = self._call_tool_success(current_server, name, arguments)
                    self.assertFalse(delivered.is_error)
                    assert delivered.structured_content is not None
                    self.assertIn("response_truncated", delivered.structured_content)
                    self.assertIs(
                        delivered.structured_content["response_truncated"], False
                    )

    def test_mail_search_never_carries_response_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            summary_delivered = self._call_tool_success(
                server, "mail_search", {"sender": "boundary@example.com", "limit": 10}
            )
            full_delivered = self._call_tool_success(
                server,
                "mail_search",
                {
                    "sender": "boundary@example.com",
                    "limit": 10,
                    "full_body": True,
                },
            )

        for delivered in (summary_delivered, full_delivered):
            self.assertFalse(delivered.is_error)
            assert delivered.structured_content is not None
            self.assertNotIn("response_truncated", delivered.structured_content)
            self.assertIn("result", delivered.structured_content)
            rows = delivered.structured_content["result"]
            self.assertTrue(rows)
            for row in rows:
                self.assertNotIn("response_truncated", row)

    # -- per-tool ladders: mail_show / mail_thread (tasks 2.2, 2.3) -------

    @pytest.mark.slow
    def test_mail_show_ladder_reduces_body_when_result_exceeds_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_body_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_show",
                {"message_id": 1, "body_max_chars": 100000},
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        # The single record is never dropped, and its identifiers are
        # unchanged by the reduction.
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["message_id"], "<oversized-body@example.com>")

        # Reported through the existing body-truncation fields, and
        # `body_total_chars` still states the true stored length (150000),
        # not the reduced ladder step's `body_max_chars`.
        self.assertTrue(payload["body_truncated"])
        self.assertEqual(payload["body_total_chars"], 150000)
        self.assertLess(len(payload["rendered_body_text"]), 100000)

        # Budget-driven, not caller-bound: the caller asked for 100000 and
        # the ladder had to shrink below that to fit.
        self.assertTrue(payload["response_truncated"])

        # Body alone was enough to fit under this fixture's single small
        # recipient and no attachments -- neither collection was touched.
        self.assertFalse(payload["recipients_truncated"])
        self.assertFalse(payload["attachments_truncated"])

    def test_mail_show_in_budget_call_stays_unreduced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(server, "mail_show", {"message_id": 1})

        self.assertFalse(delivered.is_error)
        assert delivered.structured_content is not None
        payload = delivered.structured_content
        self.assertFalse(payload["response_truncated"])
        self.assertFalse(payload["body_truncated"])
        self.assertFalse(payload["recipients_truncated"])
        self.assertFalse(payload["attachments_truncated"])

    @pytest.mark.slow
    def test_mail_thread_ladder_drops_oldest_messages_when_result_exceeds_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_thread_fixture_db(
                Path(temp_dir), "oversized-thread.db", count=10, body_chars=20000
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_thread",
                {"identifier": 1, "limit": 10, "full_body": True},
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        # `limit` was 10 and the thread holds exactly 10 messages, so the
        # caller's own bounds selected every one of them -- any omission
        # here is budget-driven, not caller-bound.
        self.assertEqual(payload["total_messages"], 10)
        self.assertTrue(payload["response_truncated"])
        returned_messages = payload["returned_messages"]
        self.assertLess(returned_messages, 10)
        self.assertEqual(payload["omitted_earlier_count"], 10 - returned_messages)

        messages = payload["messages"]
        self.assertEqual(len(messages), returned_messages)

        # Dropped from the oldest end: the survivors are a contiguous,
        # most-recent-last suffix of the numbered thread, in ascending
        # send-time order.
        subjects = [message["subject"] for message in messages]
        expected_suffix = [
            f"Oversized thread message {n}" for n in range(11 - len(messages), 11)
        ]
        self.assertEqual(subjects, expected_suffix)

    @pytest.mark.slow
    def test_mail_thread_ladder_reduces_the_remaining_message_once_only_one_is_left(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_thread_fixture_db(
                Path(temp_dir),
                "oversized-thread-single.db",
                count=1,
                body_chars=150000,
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_thread",
                {
                    "identifier": 1,
                    "limit": 10,
                    "full_body": True,
                    "body_max_chars": 100000,
                },
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        self.assertEqual(payload["total_messages"], 1)
        self.assertEqual(payload["returned_messages"], 1)
        self.assertEqual(payload["omitted_earlier_count"], 0)
        self.assertTrue(payload["response_truncated"])

        # A single message with nowhere left to drop -- `mail_show`'s own
        # within-record order applies instead: the message is still
        # present, and its body was reduced to fit.
        message = payload["messages"][0]
        self.assertTrue(message["body_truncated"])
        self.assertEqual(message["body_total_chars"], 150000)
        self.assertLess(len(message["rendered_body_text"]), 100000)

    def test_default_invocations_produce_small_delivered_results_for_ordinary_mail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered_cases = [
                ("mail_search", {"sender": "boundary@example.com"}),
                ("mail_show", {"message_id": 1}),
                ("mail_thread", {"identifier": 1}),
                ("mail_contact_history", {"contact": "boundary@example.com"}),
                ("mail_attachments", {"name_contains": "late"}),
            ]

            for name, arguments in delivered_cases:
                with self.subTest(tool=name):
                    delivered = self._call_tool_success(server, name, arguments)
                    self.assertFalse(delivered.is_error)
                    serialized = json.dumps(delivered.model_dump(mode="json"))
                    self.assertLess(len(serialized), 50000)

    def test_mail_contact_history_excludes_domain_overmatch_and_reports_truncation(
        self,
    ) -> None:
        """anchor-recipient-domain-filter: the recipient-side boundary fix
        excludes the `mallory@acme.com.evil` over-match message from the
        `domain` aggregation (fails if the recipient condition reverts to
        the unanchored substring pattern)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_contact_history_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            result = self._call_mail_contact_history(
                server,
                domain="acme.com",
                contacts_limit=2,
                attachments_limit=1,
            )
            full_result = self._call_mail_contact_history(server, domain="acme.com")

        self.assertEqual(result["query_type"], "domain")
        self.assertEqual(result["total_messages"], 3)
        self.assertEqual(result["distinct_contacts"], 3)
        self.assertEqual(len(result["contacts"]), 2)
        self.assertTrue(result["contacts_truncated"])
        self.assertTrue(result["attachments_truncated"])
        self.assertEqual(result["attachments"]["total_count"], 2)
        self.assertNotIn(
            "mallory@acme.com.evil",
            {entry["email"] for entry in full_result["contacts"]},
        )

    def test_mail_contact_history_rejects_bad_selectors_and_returns_zero_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_contact_history_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            bad_cases = [
                {"contact": "alice@acme.com", "domain": "acme.com"},
                {},
                {"domain": "   "},
            ]
            for arguments in bad_cases:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ToolError) as ctx:
                        self._call_mail_contact_history(server, **arguments)
                    self.assertIn("contact='<email>'", str(ctx.exception))
                    self.assertIn("domain='<example.com>'", str(ctx.exception))

            overlong_selector = "a" * 321
            with self.assertRaises(ToolError) as ctx:
                self._call_mail_contact_history(server, domain=overlong_selector)
            self.assertNotIn(overlong_selector, str(ctx.exception))
            self.assertIn("320", str(ctx.exception))

            zero_result = self._call_mail_contact_history(
                server, domain="nomatch.example"
            )

        self.assertEqual(zero_result["total_messages"], 0)
        self.assertEqual(zero_result["contacts"], [])

    def test_mail_attachments_reports_outer_and_inner_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_attachment_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            outer = self._call_mail_attachments(server, name_contains="report", limit=1)
            deduped = self._call_mail_attachments(
                server,
                name_contains="report",
                dedupe="content-hash",
                carrying_messages_limit=2,
            )

        self.assertEqual(outer["total_matched"], 4)
        self.assertEqual(outer["returned"], 1)
        self.assertTrue(outer["truncated"])

        shared_hash_entry = next(
            entry
            for entry in deduped["attachments"]
            if entry["content_hash"] == "shared-hash"
        )
        self.assertEqual(shared_hash_entry["carrying_messages_total"], 3)
        self.assertTrue(shared_hash_entry["carrying_messages_truncated"])
        self.assertEqual(len(shared_hash_entry["carrying_messages"]), 2)
        self.assertEqual(
            set(shared_hash_entry["carrying_messages"][0]),
            {
                "message_db_id",
                "rfc_message_id",
                "dedup_key",
                "sent_at",
                "sender_email",
                "subject",
            },
        )

    def test_mail_attachments_rejects_bad_inputs_and_reuses_shared_day_builder(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_attachment_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            with self.assertRaisesRegex(ToolError, "Provide at least one filter"):
                self._call_mail_attachments(server)
            with self.assertRaises(ToolError) as ctx:
                self._call_mail_attachments(
                    server, name_contains="report", dedupe="sha256"
                )
            self.assertIn("dedupe", str(ctx.exception))
            self.assertIn("content-hash", str(ctx.exception))

            baseline = self._call_mail_attachments(server, name_contains="report")
            blank_bound = self._call_mail_attachments(
                server,
                name_contains="report",
                from_date="   ",
                to_date="",
            )
            self.assertEqual(baseline, blank_bound)

            with self.assertRaises(ToolError) as ctx:
                self._call_mail_attachments(
                    server,
                    name_contains="report",
                    from_date="2026-201",
                )
            date_error = str(ctx.exception)
            self.assertIn("2026-201", date_error)
            self.assertIn("from_date", date_error)
            self.assertIn("canonical, basic, or ISO week form", date_error)

            same_day = self._call_mail_attachments(
                server,
                name_contains="report",
                from_date="2026-07-09",
                to_date="2026-07-09",
            )

        self.assertEqual(
            [row["subject"] for row in same_day["attachments"]],
            ["Attachment middle"],
        )

    # -- per-tool ladders: mail_attachments / mail_contact_history --------
    # -- (tasks 2.4, 2.5) --------------------------------------------------

    def test_mail_attachments_ladder_reduces_carrying_messages_before_dropping_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_attachment_dedup_fixture_db(
                Path(temp_dir), "oversized-attachment-dedup.db", count=60
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_attachments",
                {
                    "name_contains": "shared",
                    "dedupe": "content-hash",
                    "limit": 1,
                    "carrying_messages_limit": 50,
                },
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        # Budget-driven, not caller-bound: the caller asked for 50 carrying
        # messages and the ladder had to shrink below that to fit.
        self.assertTrue(payload["response_truncated"])

        # The single deduped entry is never dropped: `total_matched` and
        # `returned` both stay at 1, and `truncated` (the entry-level flag)
        # stays false -- there was nothing above the outer `limit` to drop,
        # and the nested collection alone was enough to blow the budget.
        self.assertEqual(payload["total_matched"], 1)
        self.assertEqual(payload["returned"], 1)
        self.assertFalse(payload["truncated"])

        entry = payload["attachments"][0]
        self.assertEqual(entry["content_hash"], "shared-hash")
        # The true carrying-message count survives untruncated...
        self.assertEqual(entry["carrying_messages_total"], 60)
        self.assertTrue(entry["carrying_messages_truncated"])
        # ...while what was actually delivered shrank below the caller's
        # own 50-item request -- the reduction the ladder made.
        self.assertLess(len(entry["carrying_messages"]), 50)

    def test_mail_attachments_ladder_drops_oldest_entries_after_carrying_floor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_attachment_entries_fixture_db(
                Path(temp_dir), "oversized-attachment-entries.db", count=60
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_attachments",
                {
                    "name_contains": "distinct",
                    "limit": 50,
                    "carrying_messages_limit": 5,
                },
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        self.assertTrue(payload["response_truncated"])
        # `total_matched` keeps reporting the full match count even though
        # entries were dropped, and `truncated` (the entry-level flag) is
        # true because of it.
        self.assertEqual(payload["total_matched"], 60)
        returned = payload["returned"]
        self.assertLess(returned, 50)
        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["attachments"]), returned)

        # Dropped from the oldest end: the survivors are the most-recent
        # `returned` of the 60 distinct entries, in unchanged newest-first
        # order.
        names = [entry["attachment_name"] for entry in payload["attachments"]]
        expected_names = [f"distinct-{n}.pdf" for n in range(59, 59 - returned, -1)]
        self.assertEqual(names, expected_names)

        # Each surviving entry's own nested collection was already at its
        # floor (one carrying message) before any entry needed to drop.
        for entry in payload["attachments"]:
            self.assertEqual(entry["carrying_messages_total"], 1)
            self.assertFalse(entry["carrying_messages_truncated"])

    def test_mail_attachments_oversized_locator_exhausts_ladder_without_truncation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "oversized-attachment-locator.db"
            )
            oversized_storage_path = "C:/cas/" + "L" * 200_000
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized attachment locator",
                        "sender": {"email": "sender@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": "<oversized-attachment-locator@example.com>",
                        "attachments": [
                            {
                                "attachment_name": "pathological-locator.pdf",
                                "content_hash": "pathological-locator-hash",
                                "size_bytes": 1,
                                "mime_type": "application/pdf",
                                "storage_path": oversized_storage_path,
                            }
                        ],
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_attachments",
                {"name_contains": "pathological-locator"},
            )

        self.assertTrue(delivered.is_error)
        self.assertIsNone(delivered.structured_content)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_storage_path, text)
        self.assertNotIn(oversized_storage_path[:1000], text)

    def test_mail_contact_history_ladder_reduces_contacts_when_over_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_contact_history_fixture_db(
                Path(temp_dir), "oversized-contact-history.db", count=200
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_contact_history",
                {"domain": "oversized-domain.example", "contacts_limit": 200},
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        # The summary scalars are never reduced, and stay exactly what the
        # unreduced aggregation produced.
        self.assertEqual(payload["total_messages"], 200)
        self.assertEqual(payload["inbound_count"], 200)
        self.assertEqual(payload["outbound_count"], 0)
        self.assertEqual(payload["distinct_contacts"], 200)
        self.assertIsNotNone(payload["first_message_at"])
        self.assertIsNotNone(payload["last_message_at"])

        # Budget-driven, not caller-bound: the caller asked for 200 (the
        # declared maximum) and the ladder had to shrink below that to fit.
        self.assertTrue(payload["response_truncated"])
        self.assertTrue(payload["contacts_truncated"])
        self.assertLess(len(payload["contacts"]), 200)

        # The attachments overview was never touched -- the contacts list
        # alone was enough to fit under budget once reduced.
        self.assertFalse(payload["attachments_truncated"])

    def test_mail_contact_history_ladder_reduces_attachments_after_contacts_floor(
        self,
    ) -> None:
        """The attachments overview's own realistic maximum contribution
        cannot exceed the real 120,000-character budget by itself (see
        `_create_small_contact_history_with_attachments_fixture_db`), so
        this deliberately patches the budget down to reach the second
        reduction phase -- `mail_contact_history`'s contacts-list-first,
        attachments-overview-second order, task 2.5 -- with a fixture still
        small enough to read by eye.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = (
                self._create_small_contact_history_with_attachments_fixture_db(
                    Path(temp_dir)
                )
            )
            server = create_server(database_path=database_path)
            with patch.object(mcp_delivery_module, "MCP_RESPONSE_BUDGET_CHARS", 4000):
                delivered = self._call_tool_success(
                    server,
                    "mail_contact_history",
                    {"domain": "small-domain.example", "attachments_limit": 12},
                )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, 4000)
        assert delivered.structured_content is not None
        payload = delivered.structured_content

        self.assertEqual(payload["total_messages"], 12)
        self.assertEqual(payload["distinct_contacts"], 3)
        self.assertTrue(payload["response_truncated"])

        # Both lists actually shrank from what the caller's own bounds
        # selected -- contacts first, then the attachments overview, once
        # contacts had nothing further to give up.
        self.assertTrue(payload["contacts_truncated"])
        self.assertLess(len(payload["contacts"]), 3)
        self.assertTrue(payload["attachments_truncated"])
        self.assertEqual(payload["attachments"]["total_count"], 12)
        self.assertLess(len(payload["attachments"]["recent"]), 12)

    def test_mail_contact_history_oversized_email_exhausts_ladder_without_truncation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "oversized-contact-email.db"
            )
            oversized_email = (
                "oversized-contact-"
                + "E" * MCP_RESPONSE_BUDGET_CHARS
                + "@pathological.example"
            )
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized contact email",
                        "sender": {"email": oversized_email},
                        "to": [{"email": "sean3808@mailattic.test"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": "<oversized-contact-email@example.com>",
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_contact_history",
                {"domain": "pathological.example"},
            )

        self.assertTrue(delivered.is_error)
        self.assertIsNone(delivered.structured_content)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_email, text)
        self.assertNotIn(oversized_email[:1000], text)

    def test_server_module_keeps_attachment_export_and_cli_out_of_process(self) -> None:
        """Rewritten by issue #28, which deliberately opened attachment
        export to the MCP server. The old ``"attachment_store" not in
        source`` stood for "no export in MCP"; what it now pins is that the
        export entry is enumerable -- the server module reaches it only
        through the write accessor, and the accessor is what imports the
        store. The CLI / subprocess assertions are unchanged.
        """
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        self.assertEqual(_find_attachment_export_entry_violations(source), [])
        self.assertIn("from .mcp_writer import MailWriter", source)
        writer_source = Path(mcp_writer_module.__file__).read_text(encoding="utf-8")
        self.assertIn("from .attachment_store import", writer_source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("uv run", source)
        self.assertNotIn("bes ", source)

    def test_attachment_export_entry_guard_rejects_bypasses(self) -> None:
        """Paired check (PC-096): each mutated copy of the real server source
        reaches attachment export without the accessor and must be rejected
        on that ground; the real source alone must pass."""
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        bypasses = {
            "direct import": "from .attachment_store import export_attachments",
            "absolute import": (
                "from mailattic.attachment_store import export_attachments"
            ),
            "module import": "import mailattic.attachment_store",
            "submodule as name": "from . import attachment_store as _store",
            "re-export through accessor module": (
                "from .mcp_writer import export_attachments"
            ),
            "write-accessor module alias (Codex round 2 finding)": (
                "import mailattic.mcp_writer as writer\nwriter.export_attachments(1)"
            ),
            "cli module re-export (Codex round 2 finding)": (
                "from mailattic import cli\ncli.export_attachments(1)"
            ),
            "cli named re-export": ("from mailattic.cli import export_attachments"),
            "importlib": (
                "import importlib\n"
                "_store = importlib.import_module("
                "'mailattic.attachment_store')"
            ),
            "importlib with split module name": (
                "from importlib import import_module\n"
                "_store = import_module('mailattic.attachment_' "
                "+ 'store')"
            ),
            "__import__ with split module name": (
                "_store = __import__('mailattic.attachment_' + 'store')"
            ),
        }
        for label, bypass in bypasses.items():
            with self.subTest(bypass=label):
                self.assertNotEqual(
                    _find_attachment_export_entry_violations(
                        source + "\n\n" + bypass + "\n"
                    ),
                    [],
                )
        self.assertEqual(_find_attachment_export_entry_violations(source), [])

    def test_attachment_export_import_allowlist_matches_real_imports_exactly(
        self,
    ) -> None:
        """Bidirectional equality (issue #28, 2026-09-24 decision): the
        allowlist must equal the real module's import set, not merely be a
        superset of it -- otherwise the allowlist could silently accumulate
        slack (an entry nothing still imports) without any test noticing.
        Removing an import from the real source must turn this red.
        """
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        actual_external, actual_internal = _declared_attachment_export_imports(tree)

        self.assertEqual(actual_external, set(_EXTERNAL_IMPORT_ALLOWLIST))
        expected_internal = {
            (module_leaf, name)
            for module_leaf, names in _INTERNAL_IMPORT_ALLOWLIST.items()
            for name in names
        }
        self.assertEqual(actual_internal, expected_internal)

    def test_attachment_export_guard_rules_each_have_discriminating_power(
        self,
    ) -> None:
        """Paired mutation test with per-rule discrimination (PC-096): a
        blanket 'violations is non-empty' assertion can pass for the wrong
        reason when several rules share a failure shape. Each mutant here
        violates exactly one of the guard's two rules; the test asserts
        *which* rule fired (by message substring), and separately proves
        that rule was necessary -- disabling it alone (via
        ``disabled_rules``) must let that specific mutant through, while
        the untouched real source stays clean either way.
        """
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        mutants_by_rule = {
            _RULE_IMPORT_ALLOWLIST: {
                "direct import": ("from .attachment_store import export_attachments"),
                "whole-module from-import": "from . import attachment_store",
                "write-accessor module alias": ("import mailattic.mcp_writer as w"),
                "write-accessor re-export": (
                    "from .mcp_writer import export_attachments"
                ),
                "cli re-export": "from .cli import main",
                "importlib": "import importlib",
                "builtins module": "import builtins",
                "nested module with allowlisted leaf": (
                    "from .other.query import render_text"
                ),
                "parent-relative module with allowlisted leaf": (
                    "from ..query import render_text"
                ),
            },
            _RULE_REFLECTION: {
                "globals dunder attribute": (
                    'MailWriter.export_attachments.__globals__["export_attachments"]'
                ),
                "builtins subscript": '__builtins__["__import__"]("x")',
                "dunder import call": '__import__("x")',
                "getattr bypass": 'getattr(MailWriter, "x")',
            },
        }
        category_substring = {
            _RULE_IMPORT_ALLOWLIST: "not in import allowlist",
            _RULE_REFLECTION: "reflection",
        }

        for rule, mutants in mutants_by_rule.items():
            for label, mutant in mutants.items():
                mutated_source = source + "\n\n" + mutant + "\n"
                with self.subTest(rule=rule, mutant=label):
                    violations = _find_attachment_export_entry_violations(
                        mutated_source
                    )
                    self.assertTrue(violations, msg=f"{label!r} was not rejected")
                    self.assertTrue(
                        all(category_substring[rule] in v for v in violations),
                        msg=(
                            f"{label!r} violation(s) not categorized as "
                            f"{rule!r}: {violations}"
                        ),
                    )

                    # Rule necessity: disabling exactly the rule this mutant
                    # is supposed to trip must let it through -- proving the
                    # rule, not some other check, was doing the rejecting.
                    with_rule_disabled = _find_attachment_export_entry_violations(
                        mutated_source, disabled_rules=frozenset({rule})
                    )
                    self.assertEqual(
                        with_rule_disabled,
                        [],
                        msg=(
                            f"{label!r} still rejected with rule {rule!r} "
                            f"disabled: {with_rule_disabled}"
                        ),
                    )

        # Negative control: the real, converged source is clean under every
        # combination, including with each rule individually disabled.
        self.assertEqual(_find_attachment_export_entry_violations(source), [])
        self.assertEqual(
            _find_attachment_export_entry_violations(
                source, disabled_rules=frozenset({_RULE_IMPORT_ALLOWLIST})
            ),
            [],
        )
        self.assertEqual(
            _find_attachment_export_entry_violations(
                source, disabled_rules=frozenset({_RULE_REFLECTION})
            ),
            [],
        )

    def test_server_module_carries_no_read_only_import_boundary_violation(
        self,
    ) -> None:
        """Task 1.1/3.4: the converged server module imports nothing beyond
        the database-free allowlist from the data-access modules, never
        imports one of those modules as a module, and passes no
        ``read_only`` keyword argument anywhere -- the property design.md's
        "Enforce with an AST guard over the server module source" and the
        spec's "The server module carries no per-call-site read-only
        argument" scenario require. This is the guard's red-to-green
        cycle's green half; task 3.4 is the same assertion, closing the
        cycle task 1.1 opened before the rewiring existed.
        """
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        violations = _find_read_only_boundary_violations(source)
        self.assertEqual(
            violations,
            [],
            msg=(
                "server module violates the read-only import boundary:\n"
                + "\n".join(violations)
            ),
        )

    def test_read_only_import_boundary_guard_rejects_unknown_bypasses(
        self,
    ) -> None:
        """Task 1.2: the guard must catch a bypass it was never told about,
        not merely replay the five calls that exist today (spec > "The
        static guard rejects a bypass it was not told about"). Two mutated
        copies of the real server source, neither of which the guard's
        allowlist names explicitly.
        """
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")

        # `walk_thread` is a real query-layer read the MCP server has never
        # imported, so blocking it cannot come from replaying one of the
        # five converged call sites -- it has to come from the allowlist
        # rejecting a name the guard was never told about. The bypass also
        # carries NO `read_only` keyword on purpose: with one, the keyword
        # check alone would populate the violation list and the assertion
        # would pass even if the import allowlist were broken -- a test with
        # a 100% pass rate and zero discriminating power. Matching the
        # specific violation text pins it to the import check.
        unknown_function_bypass = source + textwrap.dedent(
            """

            from .query import walk_thread as _bypass_walk_thread

            def _unknown_bypass_read(stored_id):
                return _bypass_walk_thread(None, stored_id)
            """
        )
        unknown_violations = _find_read_only_boundary_violations(
            unknown_function_bypass
        )
        self.assertTrue(
            any("disallowed import" in violation for violation in unknown_violations),
            msg=(
                "guard failed to reject, on import-boundary grounds, a copy "
                "importing and calling a data-access function under a name "
                f"the server does not use today; violations={unknown_violations}"
            ),
        )

        qualified_access_bypass = source + textwrap.dedent(
            """

            from mailattic import query as _bypass_query_module

            def _qualified_bypass_read(stored_id):
                return _bypass_query_module.get_message(None, stored_id)
            """
        )
        qualified_violations = _find_read_only_boundary_violations(
            qualified_access_bypass
        )
        self.assertTrue(
            any(
                "imported as a name" in violation for violation in qualified_violations
            ),
            msg=(
                "guard failed to reject, on import-boundary grounds, a copy "
                "reaching a data-access function through a qualified module "
                f"reference; violations={qualified_violations}"
            ),
        )

        # Negative control: the real, converged source alone must still be
        # accepted -- a guard that rejects everything would pass both
        # discrimination assertions above for the wrong reason.
        self.assertEqual(_find_read_only_boundary_violations(source), [])

    def test_read_only_reader_forwards_arguments_and_enforces_read_only(
        self,
    ) -> None:
        """Task 2.1: :class:`ReadOnlyMailReader`'s five methods pass the
        caller's arguments through untouched and supply ``read_only=True``
        internally -- the caller never carries that setting.
        """
        reader = mcp_readonly_module.ReadOnlyMailReader(Path("C:/mail/archive.db"))
        filters = query_module.SearchFilters(keyword="term")

        with patch.object(
            mcp_readonly_module, "search_messages", return_value=[{"id": 1}]
        ) as spied:
            result = reader.search(filters=filters, limit=5, full_body=True)
        spied.assert_called_once_with(
            database_path=reader.database_path,
            filters=filters,
            limit=5,
            full_body=True,
            read_only=True,
        )
        self.assertEqual(result, [{"id": 1}])

        with patch.object(
            mcp_readonly_module, "get_message", return_value={"id": 7}
        ) as spied:
            result = reader.get_message(7)
        spied.assert_called_once_with(reader.database_path, 7, read_only=True)
        self.assertEqual(result, {"id": 7})

        with patch.object(
            mcp_readonly_module,
            "resolve_thread_message_ids",
            return_value=[1, 2],
        ) as spied:
            result = reader.resolve_thread_message_ids("<thread@example.com>")
        spied.assert_called_once_with(
            reader.database_path, "<thread@example.com>", read_only=True
        )
        self.assertEqual(result, [1, 2])

        with patch.object(
            mcp_readonly_module,
            "aggregate_contact_history",
            return_value={"query": "a@example.com"},
        ) as spied:
            result = reader.contact_history(
                contact="a@example.com",
                domain=None,
                date_from="2026-01-01",
                date_to="2026-01-31",
                attachments_limit=20,
            )
        spied.assert_called_once_with(
            database_path=reader.database_path,
            contact="a@example.com",
            domain=None,
            date_from="2026-01-01",
            date_to="2026-01-31",
            attachments_limit=20,
            read_only=True,
        )
        self.assertEqual(result, {"query": "a@example.com"})

        with patch.object(
            mcp_readonly_module, "search_attachments", return_value=[{"id": 9}]
        ) as spied:
            result = reader.search_attachments(filters=filters, dedupe="content-hash")
        spied.assert_called_once_with(
            database_path=reader.database_path,
            filters=filters,
            dedupe="content-hash",
            read_only=True,
        )
        self.assertEqual(result, [{"id": 9}])

        with patch.object(
            mcp_readonly_module,
            "get_sync_status",
            return_value={"last_sync_status": "completed"},
        ) as spied:
            result = reader.sync_status()
        spied.assert_called_once_with(reader.database_path, read_only=True)
        self.assertEqual(result, {"last_sync_status": "completed"})

        with patch.object(
            mcp_readonly_module,
            "get_attachments_by_ids",
            return_value=[{"attachment_id": 3}],
        ) as spied:
            result = reader.get_attachments_by_ids([3, 4])
        spied.assert_called_once_with(reader.database_path, [3, 4], read_only=True)
        self.assertEqual(result, [{"attachment_id": 3}])

        # Task 2.1 coverage guard: the six per-method assertions above are
        # a hardcoded list, so a seventh method added to the accessor without
        # a matching case here would go silently untested -- exactly the
        # kind of gap that let a forgotten `read_only=True` slip through in
        # the first place. Enumerate the accessor's public methods
        # dynamically and require the set this test actually exercised to
        # equal it, so adding a method without adding a case fails here.
        exercised_method_names = {
            "search",
            "get_message",
            "resolve_thread_message_ids",
            "contact_history",
            "search_attachments",
            "sync_status",
            "get_attachments_by_ids",
        }
        public_method_names = {
            name
            for name, _ in inspect.getmembers(
                mcp_readonly_module.ReadOnlyMailReader, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        self.assertEqual(
            exercised_method_names,
            public_method_names,
            msg=(
                "ReadOnlyMailReader gained or lost a public method without "
                "this test's coverage keeping pace: "
                f"exercised={sorted(exercised_method_names)!r} "
                f"public={sorted(public_method_names)!r}"
            ),
        )

    def test_reader_module_carries_no_reader_read_only_boundary_violation(
        self,
    ) -> None:
        """The real, converged accessor module respects its own read-only
        import boundary: every call it makes into a data-access function
        passes an explicit ``read_only=True`` keyword, none of the
        data-access modules is imported as a module, and no ``read_only``
        value other than the literal ``True`` appears anywhere.
        """
        source = Path(mcp_readonly_module.__file__).read_text(encoding="utf-8")
        violations = _find_reader_read_only_violations(source)
        self.assertEqual(
            violations,
            [],
            msg=(
                "accessor module violates its own read-only boundary:\n"
                + "\n".join(violations)
            ),
        )

    def test_reader_read_only_boundary_guard_rejects_unknown_bypasses(
        self,
    ) -> None:
        """The accessor-module guard must reject the specific hole this
        change closes -- a newly added method that forgets the
        ``read_only=True`` keyword -- and must reject it on that ground
        specifically, not merely because the mutated copy triggers some
        other rule. Two further mutations cover the guard's other two
        rules, and a negative control proves the guard does not reject
        everything indiscriminately.
        """
        source = Path(mcp_readonly_module.__file__).read_text(encoding="utf-8")

        # The exact silent failure this change exists to prevent: a sixth
        # method that imports and calls a real query-layer read
        # (`walk_thread`, which the accessor does not use today) and never
        # supplies `read_only` at all.
        missing_keyword_bypass = source + textwrap.dedent(
            """

            from .query import walk_thread as _bypass_walk_thread

            def _new_method_missing_read_only(self, identifier):
                return _bypass_walk_thread(self.database_path, identifier)
            """
        )
        missing_keyword_violations = _find_reader_read_only_violations(
            missing_keyword_bypass
        )
        self.assertTrue(
            any(
                "missing an explicit read_only=True keyword" in violation
                for violation in missing_keyword_violations
            ),
            msg=(
                "guard failed to reject, on the missing-keyword ground, a "
                "copy with a new method that calls a data-access function "
                f"without read_only=True; violations={missing_keyword_violations}"
            ),
        )

        # An existing call's `read_only=True` flipped to `read_only=False`.
        flipped_value_bypass = source.replace(
            "get_message(self.database_path, stored_message_id, read_only=True)",
            "get_message(self.database_path, stored_message_id, read_only=False)",
        )
        self.assertNotEqual(
            flipped_value_bypass,
            source,
            msg="fixture setup failed to locate the get_message call to flip",
        )
        flipped_value_violations = _find_reader_read_only_violations(
            flipped_value_bypass
        )
        self.assertTrue(
            any(
                "not the literal True" in violation
                for violation in flipped_value_violations
            ),
            msg=(
                "guard failed to reject, on the literal-True ground, a copy "
                "with read_only=False in place of read_only=True; "
                f"violations={flipped_value_violations}"
            ),
        )

        # A data-access function reached through a qualified module
        # reference instead of a direct imported name.
        qualified_access_bypass = source + textwrap.dedent(
            """

            from mailattic import query as _bypass_query_module

            def _qualified_bypass_read(self, stored_message_id):
                return _bypass_query_module.get_message(
                    self.database_path, stored_message_id, read_only=True
                )
            """
        )
        qualified_violations = _find_reader_read_only_violations(
            qualified_access_bypass
        )
        self.assertTrue(
            any(
                "imported as a name" in violation for violation in qualified_violations
            ),
            msg=(
                "guard failed to reject, on import-boundary grounds, a copy "
                "reaching a data-access function through a qualified module "
                f"reference; violations={qualified_violations}"
            ),
        )

        # Negative control: the real, converged source alone must still be
        # accepted -- a guard that rejects everything would pass all three
        # discrimination assertions above for the wrong reason.
        self.assertEqual(_find_reader_read_only_violations(source), [])

    def test_read_only_reader_module_documents_no_write_path(self) -> None:
        """Task 2.2: docstring review target -- the accessor module states
        it exposes no write method and no parameter disabling read-only,
        and that a future writable path is a separate accessor.
        """
        module_doc = mcp_readonly_module.__doc__ or ""
        self.assertIn("no write method", module_doc)
        self.assertIn("separate accessor", module_doc)

    def test_caller_correctable_and_environmental_errors_are_distinguished(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = self._create_boundary_fixture_db(root)
            server = create_server(database_path=database_path)
            missing_path = root / "missing.db"
            missing_server = create_server(database_path=missing_path)

            cases = [
                (
                    "unknown-id",
                    lambda: self._call_mail_show(server, message_id=999999),
                    ["mail_search"],
                    [],
                ),
                (
                    "wrong-tool-string-id",
                    lambda: self._call_mail_show(
                        server, message_id="<early@example.com>"
                    ),
                    ["mail_thread"],
                    [],
                ),
                (
                    "bad-selector",
                    lambda: self._call_mail_contact_history(
                        server, contact="a", domain="b"
                    ),
                    ["contact='<email>'", "domain='<example.com>'"],
                    [],
                ),
                (
                    "invalid-keyword",
                    lambda: self._call_mail_search(server, keyword="PEN (", limit=10),
                    ["PEN (", "uppercase AND / OR / NOT", "double-quoted phrases"],
                    [],
                ),
                (
                    "missing-db",
                    lambda: self._call_mail_search(
                        missing_server, sender="boundary@example.com", limit=10
                    ),
                    [str(missing_path)],
                    [
                        "mail_show",
                        "mail_thread",
                        "mail_contact_history",
                        "mail_attachments",
                    ],
                ),
            ]

            for label, invocation, required, forbidden in cases:
                with self.subTest(case=label):
                    with self.assertRaises(ToolError) as ctx:
                        invocation()
                    message = str(ctx.exception)
                    for fragment in required:
                        self.assertIn(fragment, message)
                    for fragment in forbidden:
                        self.assertNotIn(fragment, message)

    # -- task 3.3: error results are bounded by construction -----------------

    def test_oversized_dedupe_keyword_and_identifier_errors_stay_within_budget(
        self,
    ) -> None:
        """Today, before the shared echo cap exists, the illegal `dedupe`
        case alone delivers 160,162 characters -- past the 120,000-character
        budget -- so this assertion must fail before `cap_error_echo` is
        applied at each site (design.md > "Bound error results by
        construction rather than by measuring", "Caller echoes are
        capped").
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            oversized_dedupe = "D" * 160_000
            # Trailing unmatched "(" reproduces the same FTS5 syntax-error
            # shape `test_invalid_keyword_returns_tool_error` already relies
            # on ("PEN ("), just scaled up to 160,000 characters.
            oversized_keyword = ("A" * 159_999) + "("
            oversized_identifier = "I" * 160_000

            cases = [
                (
                    "dedupe",
                    "mail_attachments",
                    {"name_contains": "x", "dedupe": oversized_dedupe},
                    oversized_dedupe,
                ),
                (
                    "keyword",
                    "mail_search",
                    {"keyword": oversized_keyword, "limit": 10},
                    oversized_keyword,
                ),
                (
                    "identifier",
                    "mail_thread",
                    {"identifier": oversized_identifier},
                    oversized_identifier,
                ),
            ]

            for label, tool_name, arguments, offending_value in cases:
                with self.subTest(case=label):
                    delivered = self._call_tool_error(server, tool_name, arguments)
                    self.assertTrue(delivered.is_error)
                    measured = measure_call_tool_result_chars(delivered)
                    self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
                    text = delivered.content[0].text
                    assert isinstance(text, str)
                    # Prefix-preserving cap: the capped echo still begins
                    # with the offending value's own leading characters, so
                    # the caller recognizes what it sent.
                    self.assertIn(offending_value[:50], text)

    def test_oversized_content_hash_refusal_omits_hash_and_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(Path(temp_dir) / "oversized-hash.db")
            oversized_hash = "H" * 200_000
            secret_message_id_fragment = "oversized-hash-secret-message-id"
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized hash",
                        "sender": {"email": "oversized-hash@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": f"<{secret_message_id_fragment}@example.com>",
                        "attachments": [
                            {
                                "attachment_name": "huge.pdf",
                                "content_hash": oversized_hash,
                                "size_bytes": 1,
                                "mime_type": "application/pdf",
                                "storage_path": "C:/cas/huge.pdf",
                            }
                        ],
                    }
                ],
            )
            server = create_server(database_path=database_path)
            # `content_hash` is never capped (it is an identifier), so even
            # the floor ladder state (one entry, one carrying message) still
            # exceeds the budget by itself, exhausting the ladder --
            # `mail_attachments` accepts free-form content filters, so task
            # 3.2 form 2 (narrow the query) is what it emits, never form 3.
            delivered = self._call_tool_error(
                server,
                "mail_attachments",
                {"name_contains": "huge", "limit": 1, "carrying_messages_limit": 1},
            )

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_hash, text)
        self.assertNotIn(secret_message_id_fragment, text)
        # Task 3.2 form 2: narrows the query, names no sizing parameter, and
        # never takes the terminal form (`mail_attachments` always has a
        # free-form filter to fall back on).
        self.assertIn("name_contains", text)
        self.assertIn("attachment_id", text)
        self.assertNotIn("carrying_messages_limit", text)
        self.assertNotIn("no further reduction available", text)

    def test_sqlite_operational_error_diagnostic_capped_path_uncapped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            oversized_detail = "Q" * 160_000
            with patch.object(
                mcp_readonly_module,
                "search_messages",
                side_effect=sqlite3.OperationalError(oversized_detail),
            ) as patched_search:
                delivered = self._call_tool_error(
                    server, "mail_search", {"keyword": "daykeyword"}
                )
                # tasks.md 3.5, injection class: prove the injection was
                # actually reached through the accessor seam, so the
                # assertions below describe this error rather than some
                # earlier validation failure.
                patched_search.assert_called()

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        # The resolved database path is the one fragment admitted uncapped
        # in an environmental error (design.md > "The resolved database
        # path is admitted uncapped, as its own class").
        self.assertIn(str(database_path), text)
        self.assertNotIn(oversized_detail, text)

    def test_wrong_type_argument_error_stays_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_search",
                {"keyword": "daykeyword", "limit": "A" * 160_000},
            )

        # Third tier. The framework's Pydantic argument validation rejects
        # this before the tool body runs, so the message is the framework's
        # and neither construction point can bound it (spec > "Error results
        # are bounded by construction"). On the 2.x generation the rejection
        # is delivered as an ordinary error result rather than a
        # protocol-level error, which is why it is observable here at all.
        #
        # The measured length is recorded HERE, in the regression test, and
        # not in the requirement text -- inlining it there is what made the
        # previous generation's number go stale while still reading as a
        # guarantee.
        #
        # Both assertions are needed and neither substitutes for the other.
        # The budget assertion is the guarantee. The equality assertion is
        # the PIN: without it the framework could change its formatting to
        # any other in-budget length and nothing would notice, which is the
        # exact drift this tier exists to catch. The input is fixed, so the
        # length is deterministic (measured identical across repeated runs).
        # On a legitimate framework change, update the constant in the same
        # commit that bumps the mcp generation, and record the new value.
        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(
            measured,
            MCP_RESPONSE_BUDGET_CHARS,
            msg=(
                "framework argument-validation error measured "
                f"{measured} chars against the resolved mcp version, over the "
                f"{MCP_RESPONSE_BUDGET_CHARS} budget"
            ),
        )
        self.assertEqual(
            measured,
            _FRAMEWORK_VALIDATION_ERROR_MEASURED_CHARS,
            msg=(
                "framework argument-validation error length moved from the "
                f"recorded {_FRAMEWORK_VALIDATION_ERROR_MEASURED_CHARS} to "
                f"{measured}; the framework changed its message formatting, "
                "so re-measure and update the recorded value deliberately"
            ),
        )

    def test_error_builders_cap_fragments_and_validate_templates(self) -> None:
        long_fragment = "x" * (mcp_delivery_module.ERROR_ECHO_CAP_CHARS + 1)
        built = mcp_delivery_module.build_tool_error(
            ValueError,
            "Invalid {value}",
            value=long_fragment,
        )
        self.assertIsInstance(built, ValueError)
        self.assertTrue(str(built).startswith("Invalid " + "x" * 200))
        self.assertIn("...(truncated, 201 chars total)", str(built))

        short = mcp_delivery_module.build_tool_error(
            ValueError,
            "Invalid {value}",
            value="short",
        )
        self.assertEqual(str(short), "Invalid short")

        resolved_path = Path("C:/mail/" + "p" * 300)
        environmental = mcp_delivery_module.build_environment_error(
            RuntimeError,
            resolved_path,
            "Database {database_path}: {detail}",
            detail=long_fragment,
        )
        self.assertIn(str(resolved_path), str(environmental))
        self.assertNotIn(long_fragment, str(environmental))
        self.assertIn("x" * 200, str(environmental))

        with self.assertRaises(ValueError):
            mcp_delivery_module.build_tool_error(ValueError, "Missing {value}")
        with self.assertRaises(ValueError):
            mcp_delivery_module.build_tool_error(
                ValueError, "No fragments", value="unexpected"
            )

    def test_framework_error_prefix_term_covers_its_measured_addition(self) -> None:
        """Pair the static prefix term against the framework's real addition.

        The static bound counts a term for the prefix the framework puts in
        front of a raised tool error's message. Two weaker checks cannot
        establish that term and are deliberately not used here (spec >
        "Error results are bounded by construction"):

        * asserting only that the computed bound stays below the budget --
          dropping a positive term makes the bound SMALLER, so that check
          passes more easily without the prefix and can never fail for the
          reason it exists;
        * comparing the worst-case bound against some unrelated observed
          error -- the worst case carries slack from every other term at
          once (each fragment at its cap, the maximum elision digits, the
          longest tool name), so it can flip on that slack while an
          understated prefix survives undetected.

        So the comparison is paired on ONE message. The server-authored
        message is captured from the builder itself through a spy, which is
        the only source that has not already been through the framework:
        the `ToolError` the framework raises ALREADY carries the prefix, so
        using it as the "prefix-free" side would measure zero and silently
        pass. The delta between the framework's delivered text and the
        builder's own message IS the framework's addition. Neither side
        quotes `_FRAMEWORK_ERROR_PREFIX_TEMPLATE`, so the comparison cannot
        collapse into a same-origin tautology.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            # `mail_sync_status` takes no parameters at all, so no argument
            # choice can drive it into `build_tool_error` the way the other
            # five tools' required-argument or out-of-range validation does.
            # Its only builder-raised error is the missing-database
            # environment error (design.md > Implementation Contract, "A
            # missing database file surfaces as the existing environment
            # error"), so it gets its own server pointed at a database file
            # that does not exist.
            missing_sync_status_db = Path(temp_dir) / "missing-sync-status-prefix.db"
            missing_sync_status_server = create_server(
                database_path=missing_sync_status_db
            )

            # Arguments chosen so the failure is raised by THIS server's
            # builder rather than by the framework's argument validation.
            # An empty argument set would trip the framework's own
            # validation for the two tools carrying a required parameter,
            # which is the third tier's path and builds no builder message
            # at all.
            failing_arguments: dict[str, dict[str, object]] = {
                "mail_search": {},
                "mail_show": {"message_id": 999_999},
                "mail_thread": {"identifier": 999_999},
                "mail_contact_history": {},
                "mail_attachments": {},
                "mail_sync_status": {},
                "mail_attachment_export": {
                    "attachment_ids": [],
                    "dest_dir": "C:/unused",
                },
            }
            # Every tool but `mail_sync_status` raises through
            # `build_tool_error` against the shared, populated `server`;
            # `mail_sync_status` raises through `build_environment_error`
            # against its own missing-database server.
            builder_names: dict[str, str] = dict.fromkeys(
                _REGISTERED_TOOL_NAMES, "build_tool_error"
            )
            builder_names["mail_sync_status"] = "build_environment_error"
            servers_by_tool: dict[str, object] = dict.fromkeys(
                _REGISTERED_TOOL_NAMES, server
            )
            servers_by_tool["mail_sync_status"] = missing_sync_status_server

            for tool_name in _REGISTERED_TOOL_NAMES:
                with self.subTest(tool=tool_name):
                    builder_name = builder_names[tool_name]
                    real_builder = getattr(mcp_delivery_module, builder_name)
                    built_errors: list[BaseException] = []
                    spy_builder = self._spy_builder_factory(real_builder, built_errors)
                    with patch.object(
                        mcp_server_module, builder_name, side_effect=spy_builder
                    ):
                        delivered = self._call_tool_error(
                            servers_by_tool[tool_name],
                            tool_name,
                            failing_arguments[tool_name],
                        )

                    self.assertTrue(delivered.is_error)
                    delivered_text = delivered.content[0].text
                    assert isinstance(delivered_text, str)

                    self.assertEqual(
                        len(built_errors),
                        1,
                        msg=f"{tool_name}: expected exactly one builder message",
                    )
                    # The builder's own message -- never through the
                    # framework, so genuinely prefix-free.
                    server_message = str(built_errors[0])
                    self.assertTrue(
                        delivered_text.endswith(server_message),
                        msg=(
                            f"{tool_name}: delivered text does not end with the "
                            "builder's own message, so the delta is not the "
                            "framework's prefix"
                        ),
                    )

                    observed_addition = len(delivered_text) - len(server_message)
                    self.assertGreater(
                        observed_addition,
                        0,
                        msg=(
                            f"{tool_name}: measured a zero framework addition, "
                            "which means the two sides are not independent"
                        ),
                    )
                    self.assertGreaterEqual(
                        _framework_error_prefix_max_chars(),
                        observed_addition,
                        msg=(
                            f"{tool_name}: static prefix term "
                            f"{_framework_error_prefix_max_chars()} does not cover "
                            f"the framework's measured addition {observed_addition}"
                        ),
                    )

    def test_server_error_paths_use_only_approved_builders(self) -> None:
        source = Path(mcp_server_module.__file__).read_text(encoding="utf-8")
        _assert_error_construction_paths(source)

        negative_fixtures = {
            "module-defined exception": """
                class LocalError(Exception):
                    pass

                def tool():
                    raise LocalError("bad")
            """,
            "imported exception": """
                from external_errors import ImportedError

                def tool():
                    raise ImportedError("bad")
            """,
            "indirect helper": """
                def make_error():
                    return ValueError("bad")

                def tool():
                    raise make_error()
            """,
        }
        for label, fixture in negative_fixtures.items():
            with (
                self.subTest(fixture=label),
                self.assertRaisesRegex(AssertionError, r"line \d+"),
            ):
                _assert_error_construction_paths(textwrap.dedent(fixture))

        static_negative_fixtures = {
            "non-static template": """
                def tool():
                    raise build_tool_error(ValueError, object())
            """,
            "computed constant in f-string": """
                import os
                BAD_TEMPLATE = os.getenv("BAD_TEMPLATE")

                def tool():
                    raise build_tool_error(ValueError, f"{BAD_TEMPLATE}")
            """,
            "uncapped non-database path": """
                def tool(resolved_database_path, caller_path):
                    raise build_environment_error(
                        RuntimeError, caller_path, "Database: {path}", path=caller_path
                    )
            """,
        }
        for label, fixture in static_negative_fixtures.items():
            with (
                self.subTest(fixture=label),
                self.assertRaisesRegex(AssertionError, r"line \d+"),
            ):
                _assert_error_construction_paths(textwrap.dedent(fixture))

        overlong_literal = "x" * 130_000
        with self.assertRaisesRegex(AssertionError, "worst-case delivered error"):
            _assert_error_construction_paths(
                "def tool():\n"
                f"    raise build_tool_error(ValueError, {overlong_literal!r})\n"
            )

    def test_oversized_date_bound_error_keeps_correction_for_all_date_tools(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            # Keep this invalid date non-JSON so the framework's pre-parser does not
            # interpret the all-digit value as an oversized integer before the
            # tool body receives it.
            oversized_date = "x" + "9" * 159_999
            cases = (
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "from_date": oversized_date},
                    "a YYYY-MM-DD date (canonical, basic, or ISO week form)",
                ),
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "to_date": oversized_date},
                    "a YYYY-MM-DD date (canonical, basic, or ISO week form)",
                ),
                (
                    "mail_attachments",
                    {"name_contains": "report", "from_date": oversized_date},
                    "a YYYY-MM-DD date (canonical, basic, or ISO week form)",
                ),
                (
                    "mail_attachments",
                    {"name_contains": "report", "to_date": oversized_date},
                    "a YYYY-MM-DD date (canonical, basic, or ISO week form)",
                ),
                (
                    "mail_contact_history",
                    {"contact": "boundary@example.com", "date_from": oversized_date},
                    "YYYY-MM-DD",
                ),
                (
                    "mail_contact_history",
                    {"contact": "boundary@example.com", "date_to": oversized_date},
                    "YYYY-MM-DD",
                ),
            )
            for tool_name, arguments, expected_clause in cases:
                with self.subTest(tool=tool_name, arguments=arguments):
                    delivered = self._call_tool_error(server, tool_name, arguments)
                    self.assertTrue(delivered.is_error)
                    measured = measure_call_tool_result_chars(delivered)
                    self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
                    text = delivered.content[0].text
                    assert isinstance(text, str)
                    self.assertIn(oversized_date[:200], text)
                    self.assertIn(expected_clause, text)

    def test_unexpected_exception_diagnostic_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            oversized_detail = "Z" * 160_000
            with patch.object(
                mcp_readonly_module,
                "search_messages",
                side_effect=RuntimeError(oversized_detail),
            ) as patched_search:
                delivered = self._call_tool_error(
                    server, "mail_search", {"keyword": "daykeyword"}
                )
                # tasks.md 3.5, injection class.
                patched_search.assert_called()

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_detail, text)

    def test_out_of_range_sizing_bounds_fail_before_any_database_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            cases = [
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "limit": 0},
                    "limit",
                    "1..100",
                    "search_messages",
                ),
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "body_max_chars": 999},
                    "body_max_chars",
                    "1000..100000",
                    "search_messages",
                ),
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "recipients_limit": 0},
                    "recipients_limit",
                    "1..200",
                    "search_messages",
                ),
                (
                    "mail_search",
                    {"sender": "boundary@example.com", "attachments_limit": 0},
                    "attachments_limit",
                    "1..200",
                    "search_messages",
                ),
                (
                    "mail_show",
                    {"message_id": 1, "body_max_chars": 999},
                    "body_max_chars",
                    "1000..100000",
                    "get_message",
                ),
                (
                    "mail_show",
                    {"message_id": 1, "recipients_limit": 0},
                    "recipients_limit",
                    "1..200",
                    "get_message",
                ),
                (
                    "mail_show",
                    {"message_id": 1, "attachments_limit": 0},
                    "attachments_limit",
                    "1..200",
                    "get_message",
                ),
                (
                    "mail_thread",
                    {"identifier": 1, "limit": 0},
                    "limit",
                    "1..100",
                    "resolve_thread_message_ids",
                ),
                (
                    "mail_thread",
                    {"identifier": 1, "body_max_chars": 999},
                    "body_max_chars",
                    "1000..100000",
                    "resolve_thread_message_ids",
                ),
                (
                    "mail_thread",
                    {"identifier": 1, "recipients_limit": 0},
                    "recipients_limit",
                    "1..200",
                    "resolve_thread_message_ids",
                ),
                (
                    "mail_thread",
                    {"identifier": 1, "attachments_limit": 0},
                    "attachments_limit",
                    "1..200",
                    "resolve_thread_message_ids",
                ),
                (
                    "mail_contact_history",
                    {"contact": "boundary@example.com", "attachments_limit": 0},
                    "attachments_limit",
                    "1..100",
                    "aggregate_contact_history",
                ),
                (
                    "mail_contact_history",
                    {"contact": "boundary@example.com", "contacts_limit": 0},
                    "contacts_limit",
                    "1..200",
                    "aggregate_contact_history",
                ),
                (
                    "mail_attachments",
                    {"name_contains": "late", "limit": 0},
                    "limit",
                    "1..100",
                    "search_attachments",
                ),
                (
                    "mail_attachments",
                    {"name_contains": "late", "carrying_messages_limit": 0},
                    "carrying_messages_limit",
                    "1..50",
                    "search_attachments",
                ),
            ]
            callers = {
                "mail_search": self._call_mail_search,
                "mail_show": self._call_mail_show,
                "mail_thread": self._call_mail_thread,
                "mail_contact_history": self._call_mail_contact_history,
                "mail_attachments": self._call_mail_attachments,
            }

            for tool_name, arguments, parameter, expected_range, patch_name in cases:
                with self.subTest(tool=tool_name, parameter=parameter):
                    with (
                        patch.object(
                            mcp_readonly_module,
                            patch_name,
                            side_effect=AssertionError(
                                "database should not be reached"
                            ),
                        ) as patched_data_access,
                        self.assertRaises(ToolError) as ctx,
                    ):
                        callers[tool_name](server, **arguments)
                    # tasks.md 3.5, database-never-reached class: range
                    # validation must reject before the accessor seam is
                    # touched at all.
                    patched_data_access.assert_not_called()
                    self.assertIn(parameter, str(ctx.exception))
                    self.assertIn(expected_range, str(ctx.exception))

    # -- mail_search refuses rather than shortens (task 2.6) ----------------

    def _create_oversized_search_fixture_db(
        self, root: Path, filename: str, count: int, body_chars: int
    ) -> Path:
        """*count* independent messages sharing one keyword, each with a
        *body_chars*-character body, so a `full_body=True` search at a
        generous `limit` produces a result exceeding the delivery budget --
        exercising task 2.6's refuse-rather-than-degrade contract (there is
        no ladder to walk for `mail_search`: it either fits or refuses).
        """
        database_path = initialize_database(root / filename)
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        fixtures: list[dict[str, object]] = []
        for i in range(count):
            fixtures.append(
                {
                    "subject": f"Oversized search message {i}",
                    "sender": {"email": "alice@example.com"},
                    "to": [{"email": "bob@example.com"}],
                    "sent_at": (base + timedelta(minutes=i)).isoformat(),
                    "body_text": "searchoverbudget " + ("S" * body_chars),
                    "message_id": f"<oversized-search-msg-{i}@example.com>",
                }
            )
        self._persist_messages(database_path, fixtures)
        return database_path

    def test_mail_search_refuses_rather_than_shortens_when_over_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_search_fixture_db(
                Path(temp_dir), "oversized-search.db", count=30, body_chars=5000
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_search",
                {
                    "keyword": "searchoverbudget",
                    "limit": 30,
                    "full_body": True,
                },
            )

        # Over budget: an error, never a shortened list -- `mail_search`
        # has no ladder to walk, so a wrapper carrying a partial `result`
        # key would itself be the forbidden wrapper.
        self.assertTrue(delivered.is_error)
        self.assertIsNone(delivered.structured_content)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)

    def test_mail_search_in_budget_result_still_measured_and_delivered_whole(
        self,
    ) -> None:
        # Companion to the refusal test above: an in-budget invocation of
        # the same shape is still measured (task 2.6's first clause) and
        # delivers every matched row. It drives through the success entry
        # point rather than the refusal test's client, because the two
        # paths become delivered at different layers (design.md > "Drive
        # delivery assertions through the public tool-call entry point").
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_search_fixture_db(
                Path(temp_dir), "in-budget-search.db", count=3, body_chars=200
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_success(
                server,
                "mail_search",
                {
                    "keyword": "searchoverbudget",
                    "limit": 30,
                    "full_body": True,
                },
            )

        self.assertFalse(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        assert delivered.structured_content is not None
        self.assertEqual(len(delivered.structured_content["result"]), 3)

    # -- identifiers are never truncated to fit (task 4.1) -------------------

    def test_oversized_identifier_exhausts_ladder_without_truncating_it(
        self,
    ) -> None:
        """A pathological `message_id` -- never capped by
        `_cap_presentation_string` -- is carried unchanged into every
        `mail_show` ladder state, so no reduction of body, recipients, or
        attachments can ever bring the result under budget: the ladder
        exhausts and the tool refuses instead of truncating the identifier
        to fit (design.md > "Give each tool its own degradation ladder",
        carried-forward identifier/locator no-truncation rule).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "oversized-identifier.db"
            )
            oversized_message_id = "<" + "M" * 200_000 + "@example.com>"
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized identifier",
                        "sender": {"email": "oversized-identifier@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": oversized_message_id,
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(server, "mail_show", {"message_id": 1})

        # The ladder exhausts and is converted into task 3.2 form 3 (`mail_show`
        # accepts no narrowing input), not a delivered record with the
        # identifier cut short.
        self.assertTrue(delivered.is_error)
        self.assertIsNone(delivered.structured_content)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        # The terminal message names the record by its numeric stored id
        # (1) and never by the oversized RFC822 Message-ID -- there is
        # nothing here for a truncated identifier to leak through.
        self.assertNotIn(oversized_message_id, text)
        self.assertIn("Stored message id 1 cannot be delivered", text)
        self.assertNotIn("body_max_chars", text)
        self.assertNotIn("recipients_limit", text)
        self.assertNotIn("attachments_limit", text)
        self.assertNotIn(oversized_message_id[:1000], text)

    def test_mail_thread_oversized_identifier_exhausts_ladder_without_truncation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "oversized-thread-message-id.db"
            )
            oversized_message_id = "<" + "T" * 200_000 + "@example.com>"
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized thread message id",
                        "sender": {"email": "oversized-thread@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": oversized_message_id,
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(server, "mail_thread", {"identifier": 1})

        self.assertTrue(delivered.is_error)
        self.assertIsNone(delivered.structured_content)
        self.assertLessEqual(
            measure_call_tool_result_chars(delivered),
            MCP_RESPONSE_BUDGET_CHARS,
        )
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_message_id, text)
        self.assertNotIn(oversized_message_id[:1000], text)

    # -- over-budget refusal forms (task 3.2) --------------------------------

    _DEGRADABLE_TOOL_SIZING_PARAM_NAMES = (
        "body_max_chars",
        "recipients_limit",
        "attachments_limit",
        "contacts_limit",
        "carrying_messages_limit",
    )

    def test_mail_search_form1_names_untried_sizing_parameters_above_floor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_oversized_search_fixture_db(
                Path(temp_dir), "form1-search.db", count=30, body_chars=5000
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_search",
                {"keyword": "searchoverbudget", "limit": 30, "full_body": True},
            )

        self.assertTrue(delivered.is_error)
        text = delivered.content[0].text
        assert isinstance(text, str)
        # Every applicable sizing parameter for this (full_body=True)
        # invocation is above its floor, so all four are named with their
        # accepted range -- form 1, the only form `mail_search` ever emits.
        for name in (
            "limit",
            "body_max_chars",
            "recipients_limit",
            "attachments_limit",
        ):
            self.assertIn(name, text)

    def test_mail_search_full_body_true_at_numeric_floor_names_full_body(
        self,
    ) -> None:
        """Defect 2 regression: every numeric sizing parameter is already at
        its minimum, but `full_body=True` alone still pushes a single
        message over budget (its oversized `message_id` is never capped).
        The candidate-building helper used to only look for numeric
        parameters above their floor, so this exact shape produced an
        *empty* candidate set and fell through to the "every sizing
        parameter... already at its minimum" dead-end wording -- even
        though turning `full_body` off is a genuine, untried form-1
        correction. Form 1 must name `full_body` here instead.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "form1-full-body-floor.db"
            )
            oversized_message_id = "<" + "F" * 200_000 + "@example.com>"
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Full body floor form1",
                        "sender": {"email": "alice@example.com"},
                        "to": [{"email": "bob@example.com"}],
                        "sent_at": "2026-07-01T00:00:00+08:00",
                        "body_text": "searchoverbudget body",
                        "message_id": oversized_message_id,
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(
                server,
                "mail_search",
                {
                    "keyword": "searchoverbudget",
                    "limit": 1,
                    "full_body": True,
                    "body_max_chars": 1000,
                    "recipients_limit": 1,
                    "attachments_limit": 1,
                },
            )

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertIn("full_body", text)
        self.assertNotIn("already at its minimum", text)
        self.assertNotIn("limit (currently", text)
        self.assertNotIn("body_max_chars", text)
        self.assertNotIn("recipients_limit", text)
        self.assertNotIn("attachments_limit", text)

    def test_mail_search_full_body_false_refusal_omits_dead_end_parameters(
        self,
    ) -> None:
        def _oversized_summary_row(index: int) -> dict[str, object]:
            return {
                "id": index,
                "subject": "S" * 500,
                "sender_name": "Sender Name",
                "sender_email": "alice@example.com",
                "sent_at": "2026-07-01T00:00:00+08:00",
                "received_at": "2026-07-01T00:00:01+08:00",
                "pst_source": "archive.pst",
                "folder_path": "F" * 500,
                "attachments": [],
                "snippet": "N" * 300,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_boundary_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)
            with patch.object(
                mcp_readonly_module,
                "search_messages",
                return_value=[_oversized_summary_row(i) for i in range(200)],
            ) as patched_search:
                delivered = self._call_tool_error(
                    server,
                    "mail_search",
                    {"sender": "alice@example.com", "limit": 100, "full_body": False},
                )
                # tasks.md 3.5, injection class: the oversized rows must have
                # travelled through the accessor seam for the refusal below
                # to be the one under test.
                patched_search.assert_called()

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        # `full_body=False` never loads a body or recipients, so naming
        # either would be the same dead end as naming a parameter already
        # at its floor -- both excluded from form 1's named set.
        self.assertNotIn("body_max_chars", text)
        self.assertNotIn("recipients_limit", text)

    def test_mail_thread_form3_names_record_and_no_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = initialize_database(
                Path(temp_dir) / "oversized-thread-identifier.db"
            )
            oversized_message_id = "<" + "T" * 200_000 + "@example.com>"
            self._persist_messages(
                database_path,
                [
                    {
                        "subject": "Oversized thread identifier",
                        "sender": {"email": "oversized-thread@example.com"},
                        "to": [{"email": "reader@example.com"}],
                        "sent_at": "2026-07-21T09:00:00+08:00",
                        "body_text": "body",
                        "message_id": oversized_message_id,
                    }
                ],
            )
            server = create_server(database_path=database_path)
            delivered = self._call_tool_error(server, "mail_thread", {"identifier": 1})

        self.assertTrue(delivered.is_error)
        measured = measure_call_tool_result_chars(delivered)
        self.assertLessEqual(measured, MCP_RESPONSE_BUDGET_CHARS)
        text = delivered.content[0].text
        assert isinstance(text, str)
        self.assertNotIn(oversized_message_id, text)
        # `mail_thread` accepts no narrowing input, so exhaustion is always
        # form 3, naming the single message it converged to by numeric id.
        self.assertIn("message 1,", text)
        for name in self._DEGRADABLE_TOOL_SIZING_PARAM_NAMES:
            self.assertNotIn(name, text)
        # No other registered tool is named as a fetch route (the message's
        # own name and the host `bes` CLI names are expected and excluded).
        for tool_name in _REGISTERED_TOOL_NAMES:
            if tool_name != "mail_thread":
                self.assertNotIn(tool_name, text)

    def test_mail_contact_history_form2_form3_boundary_is_decidable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = self._create_contact_history_fixture_db(Path(temp_dir))
            server = create_server(database_path=database_path)

            cases = [
                ("neither bound", {"domain": "acme.com"}, False),
                (
                    "only date_from",
                    {"domain": "acme.com", "date_from": "2026-07-01"},
                    False,
                ),
                (
                    "only date_to",
                    {"domain": "acme.com", "date_to": "2026-07-01"},
                    False,
                ),
                (
                    "both, two days",
                    {
                        "domain": "acme.com",
                        "date_from": "2026-07-01",
                        "date_to": "2026-07-02",
                    },
                    False,
                ),
                (
                    "both, same day",
                    {
                        "domain": "acme.com",
                        "date_from": "2026-07-01",
                        "date_to": "2026-07-01",
                    },
                    True,
                ),
            ]
            with patch.object(mcp_delivery_module, "MCP_RESPONSE_BUDGET_CHARS", 100):
                for label, arguments, expect_terminal in cases:
                    with self.subTest(case=label):
                        delivered = self._call_tool_error(
                            server, "mail_contact_history", arguments
                        )
                        self.assertTrue(delivered.is_error)
                        text = delivered.content[0].text
                        assert isinstance(text, str)
                        if expect_terminal:
                            # Task 3.2 form 3: both bounds present and
                            # naming the same Taipei day is the narrowest
                            # range this tool accepts -- no record named
                            # (the summary is an aggregate, not a row).
                            self.assertIn("narrowest range this tool accepts", text)
                            self.assertNotIn("Narrow the query", text)
                        else:
                            # Task 3.2 form 2: an absent bound is the
                            # widest range and therefore always narrowable.
                            self.assertIn("date_from", text)
                            self.assertIn("date_to", text)
                            self.assertNotIn("narrowest range this tool accepts", text)
                        for name in self._DEGRADABLE_TOOL_SIZING_PARAM_NAMES:
                            self.assertNotIn(name, text)
                        # No other registered tool is named as a fetch
                        # route (this message's own name and the host
                        # `bes` CLI name are expected and excluded).
                        for tool_name in _REGISTERED_TOOL_NAMES:
                            if tool_name != "mail_contact_history":
                                self.assertNotIn(tool_name, text)

    def test_mail_search_and_mail_attachments_never_reach_terminal_form(self) -> None:
        # Companion to the form2/3 boundary test above: `mail_search` and
        # `mail_attachments` always have a free-form filter to fall back
        # on, so neither ever emits the "narrowest range" / "no further
        # reduction available" terminal wording (design.md > form 2/3
        # boundary rule).
        with tempfile.TemporaryDirectory() as temp_dir:
            search_db = self._create_oversized_search_fixture_db(
                Path(temp_dir), "never-terminal-search.db", count=30, body_chars=5000
            )
            attachments_db = self._create_attachment_fixture_db(Path(temp_dir))
            search_server = create_server(database_path=search_db)
            attachments_server = create_server(database_path=attachments_db)

            search_delivered = self._call_tool_error(
                search_server,
                "mail_search",
                {"keyword": "searchoverbudget", "limit": 30, "full_body": True},
            )
            with patch.object(mcp_delivery_module, "MCP_RESPONSE_BUDGET_CHARS", 100):
                attachments_delivered = self._call_tool_error(
                    attachments_server, "mail_attachments", {"name_contains": "report"}
                )

        for delivered in (search_delivered, attachments_delivered):
            self.assertTrue(delivered.is_error)
            text = delivered.content[0].text
            assert isinstance(text, str)
            self.assertNotIn("no further reduction available", text)
            self.assertNotIn("narrowest range this tool accepts", text)


class MessagePayloadShapingTests(unittest.TestCase):
    """Group 2 (tasks 3.1, 3.2, 3.3, 4.3): the shared payload-shaping helpers
    that `mail_show` / `mail_thread` will call in a later group. No tool is
    registered or exercised here -- these tests call the helpers directly.
    """

    def _minimal_message(self, **overrides: object) -> dict[str, object]:
        message: dict[str, object] = {
            "id": 1,
            "subject": "Subject",
            "sender_name": "Sender Name",
            "sender_email": "sender@example.com",
            "sent_at": "2026-07-01T00:00:00+08:00",
            "received_at": "2026-07-01T00:00:01+08:00",
            "body_text": "<p>Hello world</p>",
            "message_id": "<msg@example.com>",
            "in_reply_to": None,
            "references": None,
            "pst_source": "archive.pst",
            "folder_path": "Inbox",
            "to": [],
            "cc": [],
            "bcc": [],
            "attachments": [],
        }
        message.update(overrides)
        return message

    def test_forbidden_and_extra_nested_keys_never_survive_shaping(self) -> None:
        message = self._minimal_message(
            logical_locator="Inbox/1",
            source_input_mode="file",
            source_requested_from="C:/mail/archive.pst",
            raw_headers="Received: from mx.example.com ...",
            to=[
                {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "logical_locator": "leaked-nested-value",
                }
            ],
            attachments=[
                {
                    "id": 10,
                    "attachment_name": "file.pdf",
                    "content_hash": "abc123",
                    "size_bytes": 100,
                    "mime_type": "application/pdf",
                    "storage_path": "C:/cas/abc123.pdf",
                    "raw_headers": "leaked-nested-value",
                }
            ],
        )

        payload = shape_message_payload(
            message,
            include_body=True,
            recipients_limit=25,
            attachments_limit=20,
        )
        serialized = json.loads(json.dumps(payload))

        allowed_top_level = {
            "id",
            "subject",
            "sender_name",
            "sender_email",
            "direction",
            "sent_at",
            "received_at",
            "message_id",
            "in_reply_to",
            "references",
            "pst_source",
            "folder_path",
            "to",
            "cc",
            "bcc",
            "attachments",
            "attachment_names",
            "rendered_body_text",
            "body_truncated",
            "body_total_chars",
            "recipients_total",
            "recipients_truncated",
            "attachments_total",
            "attachments_truncated",
            "truncated_fields",
        }
        self.assertTrue(set(serialized.keys()).issubset(allowed_top_level))
        for forbidden in (
            "body_text",
            "raw_headers",
            "logical_locator",
            "source_input_mode",
            "source_requested_from",
        ):
            self.assertNotIn(forbidden, serialized)

        self.assertEqual(len(serialized["to"]), 1)
        self.assertEqual(set(serialized["to"][0].keys()), {"name", "email"})
        self.assertEqual(len(serialized["attachments"]), 1)
        self.assertEqual(
            set(serialized["attachments"][0].keys()),
            {
                "id",
                "attachment_name",
                "content_hash",
                "size_bytes",
                "mime_type",
                "storage_path",
            },
        )

    @pytest.mark.slow
    def test_body_budget_boundary_table(self) -> None:
        # Mirrors the spec's "body budget boundary cases" example table.
        cases = [
            (500, 20000, 500, False, 500),
            (20000, 20000, 20000, False, 20000),
            (45000, 20000, 20000, True, 45000),
        ]
        for body_len, cap, expected_len, expected_truncated, expected_total in cases:
            with self.subTest(body_len=body_len, cap=cap):
                rendered, truncated, total = render_message_body(
                    "x" * body_len, new_only=False, body_max_chars=cap
                )
                self.assertEqual(len(rendered), expected_len)
                self.assertEqual(truncated, expected_truncated)
                self.assertEqual(total, expected_total)

        with self.assertRaisesRegex(ValueError, "body_max_chars"):
            render_message_body("x" * 45000, new_only=False, body_max_chars=999)
        with self.assertRaisesRegex(ValueError, "body_max_chars"):
            render_message_body("x" * 45000, new_only=False, body_max_chars=100001)

    def test_collection_bounds_report_true_totals(self) -> None:
        message = self._minimal_message(
            to=[{"name": f"R{i}", "email": f"r{i}@example.com"} for i in range(30)],
            cc=[],
            bcc=[],
            attachments=[
                {
                    "id": i,
                    "attachment_name": f"f{i}.txt",
                    "content_hash": f"h{i}",
                    "size_bytes": 1,
                    "mime_type": "text/plain",
                    "storage_path": f"/cas/{i}",
                }
                for i in range(25)
            ],
        )

        payload = shape_message_payload(
            message, include_body=False, recipients_limit=5, attachments_limit=10
        )

        self.assertEqual(len(payload["to"]), 5)
        self.assertEqual(payload["recipients_total"], {"to": 30, "cc": 0, "bcc": 0})
        self.assertTrue(payload["recipients_truncated"])
        self.assertEqual(len(payload["attachments"]), 10)
        self.assertEqual(len(payload["attachment_names"]), 10)
        self.assertEqual(payload["attachments_total"], 25)
        self.assertTrue(payload["attachments_truncated"])

    def test_untruncated_collections_report_false_and_true_totals(self) -> None:
        message = self._minimal_message(
            to=[{"name": "Alice", "email": "alice@example.com"}],
            attachments=[
                {
                    "id": 1,
                    "attachment_name": "f.txt",
                    "content_hash": "h",
                    "size_bytes": 1,
                    "mime_type": "text/plain",
                    "storage_path": "/cas/1",
                }
            ],
        )

        payload = shape_message_payload(
            message, include_body=False, recipients_limit=25, attachments_limit=20
        )

        self.assertEqual(payload["recipients_total"], {"to": 1, "cc": 0, "bcc": 0})
        self.assertFalse(payload["recipients_truncated"])
        self.assertEqual(payload["attachments_total"], 1)
        self.assertFalse(payload["attachments_truncated"])

    def test_out_of_range_bound_raises_naming_parameter_and_range(self) -> None:
        message = self._minimal_message()
        cases: list[tuple[dict[str, object], str, int, int]] = [
            (
                {"recipients_limit": 0, "attachments_limit": 20},
                "recipients_limit",
                1,
                200,
            ),
            (
                {"recipients_limit": 201, "attachments_limit": 20},
                "recipients_limit",
                1,
                200,
            ),
            (
                {"recipients_limit": 25, "attachments_limit": 0},
                "attachments_limit",
                1,
                200,
            ),
            (
                {"recipients_limit": 25, "attachments_limit": 201},
                "attachments_limit",
                1,
                200,
            ),
            (
                {
                    "recipients_limit": 25,
                    "attachments_limit": 20,
                    "body_max_chars": 999,
                },
                "body_max_chars",
                1000,
                100000,
            ),
            (
                {
                    "recipients_limit": 25,
                    "attachments_limit": 20,
                    "body_max_chars": 100001,
                },
                "body_max_chars",
                1000,
                100000,
            ),
        ]
        for kwargs, param, low, high in cases:
            with self.subTest(param=param, value=kwargs.get(param)):
                with self.assertRaises(ValueError) as ctx:
                    shape_message_payload(message, include_body=True, **kwargs)
                text = str(ctx.exception)
                self.assertIn(param, text)
                self.assertIn(f"{low}..{high}", text)

    def test_presentation_strings_are_capped_and_reported(self) -> None:
        long_subject = "S" * 600
        long_name = "N" * 250
        long_attachment_name = "A" * 350
        long_folder = "F" * 600
        long_storage_path = "P" * 1000
        long_message_id = "<" + "M" * 1000 + "@example.com>"

        message = self._minimal_message(
            subject=long_subject,
            sender_name=long_name,
            message_id=long_message_id,
            folder_path=long_folder,
            to=[{"name": long_name, "email": "alice@example.com"}],
            attachments=[
                {
                    "id": 10,
                    "attachment_name": long_attachment_name,
                    "content_hash": "h",
                    "size_bytes": 1,
                    "mime_type": "text/plain",
                    "storage_path": long_storage_path,
                }
            ],
        )

        payload = shape_message_payload(
            message, include_body=False, recipients_limit=25, attachments_limit=20
        )

        self.assertEqual(payload["subject"], long_subject[:500])
        self.assertEqual(payload["truncated_fields"]["subject"], 600)
        self.assertEqual(payload["sender_name"], long_name[:200])
        self.assertEqual(payload["truncated_fields"]["sender_name"], 250)
        self.assertEqual(payload["folder_path"], long_folder[:500])
        self.assertEqual(payload["truncated_fields"]["folder_path"], 600)

        recipient = payload["to"][0]
        self.assertEqual(recipient["name"], long_name[:200])
        self.assertEqual(payload["truncated_fields"]["to[0].name"], 250)

        attachment = payload["attachments"][0]
        self.assertEqual(attachment["attachment_name"], long_attachment_name[:300])
        self.assertEqual(
            payload["truncated_fields"]["attachments[0].attachment_name"], 350
        )
        self.assertEqual(
            payload["truncated_fields"]["attachment_names[0]"],
            350,
        )

        # Identifiers and locators are never truncated, at any length, and
        # never named in `truncated_fields`.
        self.assertEqual(payload["message_id"], long_message_id)
        self.assertEqual(attachment["storage_path"], long_storage_path)
        self.assertNotIn("message_id", payload["truncated_fields"])
        self.assertNotIn("storage_path", payload["truncated_fields"])
        self.assertNotIn("attachments[0].storage_path", payload["truncated_fields"])

    def test_untruncated_record_carries_no_truncated_fields_key(self) -> None:
        message = self._minimal_message(
            to=[{"name": "Alice", "email": "alice@example.com"}],
        )

        payload = shape_message_payload(
            message, include_body=False, recipients_limit=25, attachments_limit=20
        )

        self.assertNotIn("truncated_fields", payload)

    def test_include_body_false_omits_body_fields_entirely(self) -> None:
        message = self._minimal_message(body_text="<p>should not appear</p>")

        payload = shape_message_payload(
            message, include_body=False, recipients_limit=25, attachments_limit=20
        )

        self.assertNotIn("rendered_body_text", payload)
        self.assertNotIn("body_truncated", payload)
        self.assertNotIn("body_total_chars", payload)

    def test_include_body_true_reuses_cli_new_only_quote_stripping(self) -> None:
        message = self._minimal_message(
            body_text="New content\n\n> quoted history line",
        )

        payload = shape_message_payload(
            message,
            include_body=True,
            recipients_limit=25,
            attachments_limit=20,
            new_only=True,
        )

        self.assertEqual(payload["rendered_body_text"], "New content")
        self.assertFalse(payload["body_truncated"])
