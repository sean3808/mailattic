# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""
Stage 4 ball-in-court detection for mailattic find tasks.

Walks the thread containing a seed db_id, finds the chronologically last
message, and reports whether the latest message is from an external party
containing question/follow-up signals — meaning we (mailattic.test) probably
owe a reply.

This is heuristic. Polite closing phrases ("kindly let me know") trigger
the signal too; the report should let the human user decide.

Uses the released global BES. Database defaults are resolved by BES.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# `bes thread` returns only the earliest 100 messages (and warns on stderr).
THREAD_CAP = 100
DEFAULT_TRIGGERS = ["?", "？", "請", "kindly", "please", "報價", "quote", "催"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--seed-db-id",
        type=int,
        required=True,
        help="db id of any message in the target thread",
    )
    parser.add_argument(
        "--no-thread",
        action="store_true",
        help="Check only the seed message instead of walking the thread (use when "
        "checking a standalone message)",
    )
    parser.add_argument(
        "--database", default=None, help="Override MAILATTIC_DB env var"
    )
    args = parser.parse_args()

    try:
        tool_bin = subprocess.run(
            [shutil.which("uv") or "uv", "tool", "dir", "--bin"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        bes = shutil.which(str(Path(tool_bin) / "bes")) if tool_bin else None
        if not bes:
            raise FileNotFoundError(
                "Released BES not found in uv tool bin; install BES first"
            )
        command = [
            bes,
            "show" if args.no_thread else "thread",
            str(args.seed_db_id),
            "--format",
            "json",
        ]
        if args.database is not None:
            command.extend(["--database", args.database])
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8"
        )
        messages = json.loads(result.stdout)
        msgs = [messages] if args.no_thread else messages
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return exc.returncode
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not msgs:
        print("thread empty or seed not found", file=sys.stderr)
        return 1

    if not args.no_thread and len(msgs) >= THREAD_CAP:
        print(
            f"[indeterminate] thread reached the {THREAD_CAP}-message CLI cap; the "
            "latest message is not visible, so who owes a reply cannot be decided"
        )
        return 3

    # Latest by instant, not by string: stored sent_at keeps its own offset, and
    # the released bes may still hand back string order. Naive = UTC, the same
    # reading the query layer gives a stored sent_at without an offset
    # (D-20260723-1); ties fall back to list position.
    def _instant(pair: tuple[int, dict]) -> tuple[float, int] | None:
        pos, m = pair
        try:
            dt = datetime.fromisoformat(m.get("sent_at") or "")
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (dt.timestamp(), pos)

    # A message without a readable sent_at could be the latest one; ranking it
    # anywhere would be a guess, so a thread containing one gets no verdict.
    undated = [m["id"] for m in msgs if _instant((0, m)) is None]
    if undated and len(msgs) > 1:
        print(
            "[indeterminate] sent_at is missing or unreadable for "
            + ", ".join(f"db_id={i}" for i in undated)
            + "; the latest message cannot be identified, so who owes a reply "
            "cannot be decided"
        )
        return 3

    last = max(enumerate(msgs), key=lambda pair: _instant(pair) or (0.0, pair[0]))[1]
    sender = (last.get("sender_email") or "").lower()
    body = (last.get("body_text") or "").lower()
    direction = last.get("direction")
    inbound = direction == "inbound"
    matched_triggers = [t for t in DEFAULT_TRIGGERS if t in body]

    print(f"thread size: {len(msgs)} msg(s)")
    print(
        f"last msg: db_id={last['id']} sent_at={(last.get('sent_at') or '')[:19]} "
        f"from={sender}"
    )

    if inbound and matched_triggers:
        print(f"[BALL-IN-COURT] external sender + signals matched: {matched_triggers}")
        print("  -> we likely owe a reply; surface this db_id to the user")
        return 0
    elif inbound:
        print("[likely-handled] external sender but no question/follow-up signals")
        return 0
    elif direction != "outbound":
        print("[unknown-direction] last msg has no known direction; cannot decide")
        return 0
    else:
        print(f"[our-side] last msg sent by us ({sender}); thread is in their court")
        return 0


if __name__ == "__main__":
    sys.exit(main())
