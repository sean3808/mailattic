"""thread_key — pure in-memory union-find for connected-component thread keys.

Computes {db_id -> thread_key} mappings from message header fields without
touching the database.  Designed to be reused by:

- backfill (task 3): bulk-compute thread_key for all existing rows
- ingestion (task 6): assign/merge thread_key for a newly inserted message

Parsing semantics are intentionally identical to walk_thread() in query.py:
- references_header is parsed via the reused _tokenize_references() helper
- in_reply_to and each reference token are normalised with .strip("<>")
- Message-ID normalisation: .strip("<>") (same as walk_thread batch lookup)

This ensures connected-component membership here == connected-component
membership from walk_thread's BFS, minus the LIKE substring false-positives
that parser-based matching corrects.  That correction is the *only* deliberate
semantic difference (design.md Decision 2).

Reference loops terminate naturally: union-find does not expand already-unified
nodes, and the algorithm is non-recursive.
"""

from __future__ import annotations

from collections.abc import Iterable

# Import parsing helpers from query.py — do NOT reimplement.
from .query import _tokenize_references  # noqa: PLC2701 (internal helper, by design)

# ---------------------------------------------------------------------------
# Union-Find (path-compressed, union-by-rank)
# ---------------------------------------------------------------------------


class _UnionFind:
    """Weighted quick-union with path compression.

    Nodes are arbitrary hashable keys (used here as bare Message-ID strings).
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        """Return the canonical root of x, with path compression."""
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        """Merge the components containing a and b."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Union by rank
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


type _ThreadRow4 = tuple[int, str | None, str | None, str | None]
type _ThreadRow6 = tuple[
    int,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]


def compute_thread_keys(
    messages: Iterable[_ThreadRow4 | _ThreadRow6],
) -> dict[int, int]:
    """Compute a {db_id -> thread_key} mapping for an iterable of messages.

    Parameters
    ----------
    messages:
        An iterable of ``(db_id, message_id, in_reply_to, references_header)``
        tuples, or extended
        ``(db_id, message_id, in_reply_to, references_header, dedup_key,
        thread_bridge_target)`` tuples.  All fields except ``db_id`` may be
        ``None`` or empty.

    Returns
    -------
    dict[int, int]
        Maps every input ``db_id`` to its ``thread_key``.  The ``thread_key``
        for a connected component is the **minimum db_id** among all members of
        that component (deterministic, monotone, never drifts on re-runs).

        A message with ``NULL``/empty ``message_id`` and no resolvable
        references becomes a singleton whose ``thread_key`` equals its own
        ``db_id``.

    Algorithm
    ---------
    1. Materialise the input so we can do two passes.
    2. First pass: build ``mid_to_dbids`` (normalised Message-ID → list of
       db_ids) and union-find nodes keyed by normalised Message-ID.
    3. Second pass: for each row, union its ``message_id`` with every id that
       appears in ``in_reply_to`` and ``references_header`` (parsed and
       normalised identically to walk_thread).
    4. Resolve every row's Message-ID (or synthetic singleton key) to its
       union-find root, then map roots → minimum db_id across all members →
       final {db_id -> thread_key}.

    Parsing normalisation (mirrors query.py walk_thread exactly):
    - ``in_reply_to.strip("<>")``
    - ``_tokenize_references(references_header)`` → each token ``.strip("<>")``
    - Message-ID: ``.strip("<>")``
    """
    # Materialise so we can iterate twice.
    raw_rows = list(messages)
    rows: list[_ThreadRow6] = [
        (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4] if len(row) > 4 else None,
            row[5] if len(row) > 5 else None,
        )
        for row in raw_rows
    ]

    uf = _UnionFind()

    # Synthetic node prefix for singletons without a real Message-ID.
    # Using "dbid:<n>" avoids collision with real Message-IDs (which always
    # contain "@" per RFC 2822; "dbid:" prefix contains no "@").
    _SINGLETON_PREFIX = "dbid:"

    def _node_for_row(db_id: int, message_id: str | None) -> str:
        """Return the union-find node key for this row."""
        mid = (message_id or "").strip("<>").strip()
        if mid:
            return mid
        return f"{_SINGLETON_PREFIX}{db_id}"

    anchor_to_node: dict[str, str] = {}

    def _register_anchor(anchor: str | None, node: str) -> None:
        value = (anchor or "").strip()
        if not value:
            return
        anchor_to_node[value] = node
        stripped = value.strip("<>").strip()
        if stripped:
            anchor_to_node[stripped] = node

    # Pass 1: register all nodes in the union-find structure.
    for db_id, message_id, _irt, _refs, dedup_key, _bridge_target in rows:
        node = _node_for_row(db_id, message_id)
        uf.find(node)  # ensure node exists in union-find
        _register_anchor(message_id, node)
        _register_anchor(dedup_key, node)

    # Pass 2: union edges (identical to walk_thread's BFS edge relation).
    for (
        db_id,
        message_id,
        in_reply_to,
        references_header,
        _dedup_key,
        bridge_target,
    ) in rows:
        src_node = _node_for_row(db_id, message_id)

        # Collect all referenced Message-IDs for this row.
        ref_ids: list[str] = []

        if in_reply_to:
            ref_ids.append(in_reply_to.strip("<>"))

        for tok in _tokenize_references(references_header):
            ref_ids.append(tok.strip("<>"))

        for ref_id in ref_ids:
            ref_id = ref_id.strip()
            if not ref_id:
                continue
            # Ensure the referenced node exists in union-find
            # (it may or may not correspond to a known db_id).
            uf.find(ref_id)
            uf.union(src_node, ref_id)

        if bridge_target:
            target_node = anchor_to_node.get(bridge_target.strip())
            if target_node is None:
                target_node = anchor_to_node.get(bridge_target.strip("<>").strip())
            if target_node is not None:
                uf.union(src_node, target_node)

    # Resolve: root → minimum db_id across all rows in that component.
    root_to_min_dbid: dict[str, int] = {}
    for db_id, message_id, _irt, _refs, _dedup_key, _bridge_target in rows:
        node = _node_for_row(db_id, message_id)
        root = uf.find(node)
        if root not in root_to_min_dbid or db_id < root_to_min_dbid[root]:
            root_to_min_dbid[root] = db_id

    # Build final mapping.
    result: dict[int, int] = {}
    for db_id, message_id, _irt, _refs, _dedup_key, _bridge_target in rows:
        node = _node_for_row(db_id, message_id)
        root = uf.find(node)
        result[db_id] = root_to_min_dbid[root]

    return result
