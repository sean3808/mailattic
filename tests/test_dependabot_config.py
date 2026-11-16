"""Guards for `.github/dependabot.yml` configuration."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"


def _load_dependabot_config() -> dict:
    assert DEPENDABOT_CONFIG.is_file(), f"Missing {DEPENDABOT_CONFIG}"
    data = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_dependabot_config_version_and_ecosystems() -> None:
    """Dependabot v2 configures monthly grouped updates for github-actions and uv."""
    config = _load_dependabot_config()
    assert config.get("version") == 2

    updates = config.get("updates")
    assert isinstance(updates, list)

    by_ecosystem = {
        entry["package-ecosystem"]: entry
        for entry in updates
        if isinstance(entry, dict) and "package-ecosystem" in entry
    }
    assert {"github-actions", "uv"}.issubset(by_ecosystem.keys())

    for ecosystem in ("github-actions", "uv"):
        entry = by_ecosystem[ecosystem]
        assert entry.get("directory") == "/"
        assert entry.get("schedule", {}).get("interval") == "monthly"
        assert "dependencies" in entry.get("labels", [])

        groups = entry.get("groups")
        assert isinstance(groups, dict) and groups, (
            f"{ecosystem} must define at least one update group"
        )
        assert any(
            isinstance(group, dict) and "*" in group.get("patterns", [])
            for group in groups.values()
        ), f"{ecosystem} must group all updates with patterns: ['*']"
