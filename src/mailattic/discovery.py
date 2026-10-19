from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .models import PstSource


def discover_pst_sources(
    directory_inputs: Sequence[str | Path] | None = None,
    file_inputs: Sequence[str | Path] | None = None,
) -> list[PstSource]:
    if not directory_inputs and not file_inputs:
        raise ValueError("At least one directory or file input is required.")

    sources: dict[Path, PstSource] = {}

    for directory_input in directory_inputs or ():
        requested_from = Path(directory_input).expanduser()
        if not requested_from.exists():
            raise FileNotFoundError(f"Directory input does not exist: {requested_from}")
        if not requested_from.is_dir():
            raise NotADirectoryError(
                f"Directory input is not a directory: {requested_from}"
            )

        resolved_directory = requested_from.resolve()
        for candidate in sorted(
            path
            for path in requested_from.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pst"
        ):
            resolved_path = candidate.resolve()
            sources.setdefault(
                resolved_path,
                PstSource(
                    path=resolved_path,
                    input_mode="directory",
                    requested_from=resolved_directory,
                ),
            )

    for file_input in file_inputs or ():
        candidate = Path(file_input).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"File input does not exist: {candidate}")
        if not candidate.is_file():
            raise ValueError(f"File input is not a file: {candidate}")
        if candidate.suffix.lower() != ".pst":
            raise ValueError(f"File input is not a PST file: {candidate}")

        resolved_path = candidate.resolve()
        sources[resolved_path] = PstSource(
            path=resolved_path,
            input_mode="file",
            requested_from=resolved_path,
        )

    ordered_paths = sorted(sources, key=lambda item: str(item).lower())
    return [sources[path] for path in ordered_paths]
