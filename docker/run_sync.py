from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace/src")

from mailattic.attachment_store import resolve_store_root
from mailattic.imap_runtime import run_logged_sync_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_sync.py",
        description="Run one mbsync + maildir ingest cycle inside the container.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--maildir-root", type=Path, required=True)
    parser.add_argument(
        "--template-path",
        type=Path,
        default=Path("/opt/mailattic/mbsyncrc.template"),
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=Path("/workspace/.mailattic-work/mbsync/mbsyncrc"),
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("/workspace/.mailattic-work/logs"),
    )
    parser.add_argument(
        "--attachment-root", type=Path, default=None, dest="attachment_root"
    )
    parser.add_argument("--folder", default=None)
    parser.add_argument("--full-resync", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    attachment_root = resolve_store_root(
        args.database,
        cli_override=args.attachment_root,
    )
    result = run_logged_sync_job(
        database_path=args.database,
        maildir_root=args.maildir_root,
        template_path=args.template_path,
        config_path=args.config_path,
        log_dir=args.log_dir,
        attachment_root=attachment_root,
        folder=args.folder,
        full_resync=args.full_resync,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
