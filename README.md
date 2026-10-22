# mailattic

[繁體中文](README.zh-TW.md)

mailattic turns years of Outlook PST archives plus an incremental IMAP sync into one local SQLite database with full-text search. AI agents query it through a CLI, a Python API, or an MCP server.

It was built for one long-running mailbox on a self-hosted mail server that only keeps recent mail.

## Quick start

```powershell
git clone https://github.com/sean3808/mailattic.git
cd mailattic
uv sync
uv run bes init-db --database .\mailattic.db
uv run bes import --database .\mailattic.db --maildir C:\path\to\maildir-account --account-email you@example.com
uv run bes search --database .\mailattic.db --keyword '"PEN-100" AND quote' --sender alice@example.com --limit 10 --json
uv run bes thread 1 --database .\mailattic.db --format json
```

You need Windows 11, Python 3.13, and [`uv`](https://docs.astral.sh/uv/). The import above reads a maildir and does not use Outlook. For PST import, read [Requirements](#requirements) first.

### Use `bes` from any directory

Agents in other projects call a global `bes`. Install it from the clone, then point it at your database:

```powershell
pwsh -ExecutionPolicy Bypass -File scripts\reinstall-global.ps1
setx MAILATTIC_DB C:\path\to\mailattic.db
```

The global `bes` is a snapshot of the build, so run the script again after you pull new code. Removal and the database path order are in [`docs/global-install-and-release.md`](docs/global-install-and-release.md). To register the MCP server with Claude Desktop, add a stdio server to `claude_desktop_config.json` whose command is `uv run --project <repo> python -m mailattic.mcp_server` (quit Claude Desktop from the tray before editing the file, then restart it).

## Why query an archive

Most email tools for AI agents are live proxies. The agent asks, the server connects to IMAP or Gmail, and the answer can only come from mail the server still holds. That fails for a quote from five years ago, for a mailbox that was only ever downloaded over POP, and for old mail on a server with a retention limit. The only copy of that mail is an archive on someone's disk, and that archive is what mailattic queries.

## Three interfaces

| Interface | For | Notes |
|---|---|---|
| CLI `bes` | Coding agents and scripts on the same machine | `search`, `show`, `thread`, `attachments`, `contact-history`, `sync-imap`, `draft-reply`, `send-draft`, and more. Query subcommands can print JSON; the output options of each subcommand are in [`docs/cli-reference.md`](docs/cli-reference.md) |
| Python API | In-process callers | `search_messages`, `SearchFilters`, `get_message` |
| MCP server `bes-mcp` | Sandboxed assistants that cannot run a host CLI | Six read tools, plus attachment export, draft reply, and send |

All three run the same query code, so a date filter or a keyword means the same thing in each.

```python
from pathlib import Path
from mailattic.query import SearchFilters, get_message, search_messages

results = search_messages(
    database_path=Path("mailattic.db"),
    filters=SearchFilters(keyword="contract", sender="alice@example.com"),
    limit=20,
)
message = get_message(Path("mailattic.db"), results[0]["id"])
```

## Design choices

### Keyword search (SQLite FTS5)

Business mail is searched by things that must match exactly: part numbers, order numbers, names, addresses, dates. A hit is a message that contains the words you searched for, so results are easy to check. The cost is that a query must use words that appear in the mail; there is no semantic recall. The choice was made for a single archive on one machine.

### The agent drafts, a person approves, and the tool sends what was approved

A reply is written to a draft file first, and the draft has a SHA-256 fingerprint. An agent sends by passing that fingerprint, so a draft that changed after review is refused. At a real terminal a person can type an authorisation phrase instead. The tool cannot know whether the person was really asked. That part is the agent's duty, and the documentation says so.

### Windows first

PST import reads through a running Outlook profile, and the test launcher is PowerShell. Maildir import (`bes import --maildir`) needs neither Outlook nor Windows-specific tools.

## Limits

- Developed and tested on Windows 11 only. Cross-platform use and support are not promised.
- The real database, attachments, and credentials were never in version control. Addresses in docs and tests are placeholders such as `mailattic.test` and `example.com`.
- The commit history starts fresh here. The earlier private history is not included.
- The repository ships no data, so you have to import your own. A small de-identified sample database is planned.
- One database is one person's mailbox. Sharing one archive so that several mailboxes are searched together, needs new rules for "which addresses are us" and for access control, and none of that is built.
- Not in scope: enterprise mail server, eDiscovery, GUI client, push notifications, semantic vector search, attachment OCR, task tracking.

## Requirements

- Windows 11, Python 3.13, [`uv`](https://docs.astral.sh/uv/)
- SQLite with FTS5
- PST import only: a running Outlook with the target PST already mounted. The importer reads the store and never mounts, unmounts, or changes it. Run the smallest PST first; the run summary and the stop rules are in [`docs/cli-reference.md`](docs/cli-reference.md).

## Documentation

| Question | Go to |
|---|---|
| Every subcommand, flag, and query rule | [`docs/cli-reference.md`](docs/cli-reference.md), `uv run bes <cmd> --help` |
| MCP tools and their limits | [`docs/mcp-server-contract.md`](docs/mcp-server-contract.md) |
| Database tables | [`docs/db-schema.md`](docs/db-schema.md) |
| Global install, release, and scheduling | [`docs/global-install-and-release.md`](docs/global-install-and-release.md) |
| Project rules, architecture map, behaviour contracts | [`AGENTS.md`](AGENTS.md) |
| Exact meaning of a project term | [`GLOSSARY.md`](GLOSSARY.md) |
| Why a design was chosen | [`docs/decisions.md`](docs/decisions.md) (records are written in Traditional Chinese) |
| All documents | [`docs/INDEX.md`](docs/INDEX.md) |

## Acknowledgements

The development workflow uses Matt Pocock's [skills](https://github.com/mattpocock/skills).

## License

[MIT](LICENSE). Maintained by [@sean3808](https://github.com/sean3808).
