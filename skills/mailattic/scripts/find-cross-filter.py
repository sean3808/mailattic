"""
Stage 3 FTS + LIKE cross-filter for mailattic find tasks.

Combines a MUST keyword (must appear in body or subject) with a list of CONTEXT
LIKE patterns (any one must appear in body) within a date window, then prints
snippets around CONTEXT keyword hits so the caller AI can present candidates to
the user.

Why this exists separately from `bes search`:
- FTS-only ranking gets drowned by repeated-subject threads ("RE: RE: RE: ...").
- LIKE cross-filter against body_text + subject finds the keyword phrase even
  when it only appears in a quoted block deep in the thread.
- The output format (200-char snippets around context keyword hits) lets the
  user scan candidates and pick a db_id without the script auto-selecting.

Defaults MAILATTIC_DB env var → cwd `mailattic.db`.
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def _recipient_domain_match(recipient_text: str | None, domain: str | None) -> int:
    """Same boundary-aware rule as core storage._recipient_domain_match."""
    if not recipient_text or not domain:
        return 0
    pattern = "@" + re.escape(domain) + r"(?![\w.-])"
    return 1 if re.search(pattern, recipient_text, re.IGNORECASE) else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--must",
        required=True,
        help="Required keyword (appears in body_text OR subject)",
    )
    parser.add_argument(
        "--context",
        required=True,
        help="Comma-separated context keywords; ANY one must appear in body_text. "
        "Each wrapped with %% automatically.",
    )
    parser.add_argument(
        "--from-date",
        required=True,
        help="YYYY-MM-DD, Asia/Taipei whole day, inclusive",
    )
    parser.add_argument(
        "--to-date",
        required=True,
        help="YYYY-MM-DD, Asia/Taipei whole day, inclusive",
    )
    parser.add_argument(
        "--snippet-keywords",
        default=None,
        help="Comma-separated keywords to anchor snippet windows. Defaults to "
        "--context list.",
    )
    parser.add_argument(
        "--participants-domain",
        action="append",
        default=[],
        help="Keep only mails whose sender or recipients are on this domain "
        "(repeatable, OR). Same meaning as `bes search --participants-domain`: "
        "exact domain, case-insensitive, no subdomains, literal (no wildcards).",
    )
    parser.add_argument(
        "--database", default=None, help="Override MAILATTIC_DB env var"
    )
    args = parser.parse_args()

    # Output encoding must not depend on the caller's environment: a zh-TW
    # Windows console defaults to cp950, and callers read this as UTF-8.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    # Same rule as the core date filters (contract #4): Taipei whole day, both
    # bounds inclusive. Standalone script, so no package import (skill ships
    # without the package).
    taipei = timezone(timedelta(hours=8))
    bounds = []
    for flag, text, end in (
        ("--from-date", args.from_date, False),
        ("--to-date", args.to_date, True),
    ):
        try:
            d = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid {flag}: {text!r}. Expected YYYY-MM-DD.", file=sys.stderr)
            return 2
        d = d.replace(hour=23, minute=59, second=59) if end else d
        bounds.append(int(d.replace(tzinfo=taipei).timestamp()))

    db = (
        args.database
        or os.environ.get("MAILATTIC_DB")
        or "mailattic.db"
    )
    if not os.path.exists(db):
        print(f"DB not found: {db}", file=sys.stderr)
        return 2

    context_list = [c.strip() for c in args.context.split(",") if c.strip()]
    snippet_kws = (
        [k.strip() for k in args.snippet_keywords.split(",") if k.strip()]
        if args.snippet_keywords
        else context_list
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.create_function("recipient_domain_match", 2, _recipient_domain_match)

    where_context = " OR ".join(["body_text LIKE ?"] * len(context_list))
    # Same meaning as core query.py participants filter: sender address ends with
    # @domain, or the domain sits on a domain boundary in the recipient text.
    domains = [d.strip().lstrip("@") for d in args.participants_domain if d.strip()]
    where_domain = ""
    if domains:
        clause = " OR ".join(
            "sender_email LIKE ? ESCAPE '\\' "
            "OR recipient_domain_match(recipient_search_text, ?)"
            for _ in domains
        )
        where_domain = f"\n          AND ({clause})"
    sql = f"""
        SELECT id, subject, sender_email, sent_at, body_text
        FROM messages
        WHERE unixepoch(sent_at) BETWEEN ? AND ?
          AND (body_text LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')
          AND ({where_context}){where_domain}
        ORDER BY sent_at ASC
    """

    # --must is a literal keyword: escape LIKE wildcards (backslash first),
    # same convention as core query._escape_like. --context stays a pattern.
    def escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    must = escape_like(args.must)
    params = [
        *bounds,
        f"%{must}%",
        f"%{must}%",
        *[f"%{c}%" for c in context_list],
    ]
    for d in domains:
        params += [f"%@{escape_like(d)}", d]

    hits = 0
    no_anchor = 0
    for r in conn.execute(sql, params):
        body = r["body_text"] or ""
        # SQL LIKE is ASCII case-insensitive, so anchor matching must be too.
        # Every SQL hit is printed: the match may be only in subject/non-ASCII
        # case variants, so fall back to the body start instead of dropping it.
        m = next(
            (
                m
                for kw in snippet_kws
                if (m := re.search(re.escape(kw), body, re.IGNORECASE))
            ),
            None,
        )
        if m:
            s, e = max(0, m.start() - 150), min(len(body), m.end() + 200)
        else:
            s, e = 0, min(len(body), 350)
            no_anchor += 1
        snip = re.sub(r"\s+", " ", body[s:e])
        print(f"[{r['id']}] {r['sent_at'][:10]} | {r['sender_email']}")
        print(f"   Subject: {r['subject']}")
        label = "" if m else "[anchor not found; body start] "
        print(f"   {label}...{snip}...\n")
        hits += 1

    print(f"# total hits: {hits}", file=sys.stderr)
    if no_anchor:
        print(
            f"# {no_anchor} hit(s) had no snippet anchor in body; showing body start",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
