"""Single dedicated read-only accessor for the MCP server.

The MCP server binds the read-only setting exactly once here, at server
construction, rather than repeating a ``read_only=True`` argument at each of
the six call sites the registered read tools use (five distinct reads, with
``mail_thread`` making two of them -- identifier resolution and per-message
hydration). :class:`ReadOnlyMailReader`
exposes no write method and no parameter that turns the read-only setting
off -- there is nothing on this object a caller could pass to obtain a
writable connection. A future writable path (for a tool like ``mail_sync`` or
``mail_send``) is a separate accessor obtained in its own way, not a flag or
mode added to this one. Do not add a ``read_only`` parameter or a write
method here; that is the change this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contact_history import aggregate_contact_history
from .mcp_delivery import _as_tool_error_type
from .query import (
    SearchFilters,
    get_attachments_by_ids,
    get_message,
    get_sync_status,
    resolve_thread_message_ids,
    search_attachments,
    search_messages,
)


@dataclass(frozen=True)
class ReadOnlyMailReader:
    """Read-only accessor bound to one resolved database path.

    One method per read a registered read tool performs. Each method
    forwards to the corresponding query-layer function with the read-only
    setting supplied internally, and returns that function's result
    unchanged. No method here catches an exception -- whatever the
    underlying query-layer function raises propagates to the caller
    unmodified, so the server's existing except ladders keep matching.
    """

    database_path: Path

    def search(
        self,
        *,
        filters: SearchFilters,
        limit: int,
        full_body: bool,
    ) -> list[dict[str, object]]:
        return search_messages(
            database_path=self.database_path,
            filters=filters,
            limit=limit,
            full_body=full_body,
            read_only=True,
        )

    def get_message(self, stored_message_id: int) -> dict[str, object]:
        return get_message(self.database_path, stored_message_id, read_only=True)

    def resolve_thread_message_ids(self, identifier: int | str) -> list[int]:
        return resolve_thread_message_ids(
            self.database_path, identifier, read_only=True
        )

    def contact_history(
        self,
        *,
        contact: str | None,
        domain: str | None,
        date_from: str | None,
        date_to: str | None,
        attachments_limit: int,
    ) -> dict[str, object]:
        try:
            return aggregate_contact_history(
                database_path=self.database_path,
                contact=contact,
                domain=domain,
                date_from=date_from,
                date_to=date_to,
                attachments_limit=attachments_limit,
                read_only=True,
            )
        except ValueError as exc:
            raise _as_tool_error_type(ValueError)(str(exc)) from exc

    def search_attachments(
        self,
        *,
        filters: SearchFilters,
        dedupe: str | None,
    ) -> list[dict[str, object]]:
        return search_attachments(
            database_path=self.database_path,
            filters=filters,
            dedupe=dedupe,
            read_only=True,
        )

    def get_attachments_by_ids(
        self, attachment_ids: list[int]
    ) -> list[dict[str, object]]:
        return get_attachments_by_ids(
            self.database_path, attachment_ids, read_only=True
        )

    def sync_status(self) -> dict[str, object]:
        return get_sync_status(self.database_path, read_only=True)
