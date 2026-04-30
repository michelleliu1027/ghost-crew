"""Multi-tenant user configuration loader."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class UserConfig:
    name: str
    slack_user_id: str
    slack_user_token: str
    review_channel_id: str
    github_repos: list[str] = field(default_factory=list)
    google_docs: list[str] = field(default_factory=list)
    tone: str = "professional but casual"
    instructions: str = ""
    tracking_doc_id: str = ""
    digest_cron: str = "0 17 * * 5"
    digest_channel: str = "DM"
    exclude_dm_from: list[str] = field(default_factory=list)


def _resolve_env(value: str) -> str:
    """Resolve ${ENV_VAR} references in config values."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        return os.environ.get(env_key, value)
    return value


def load_user_config(config_path: Path) -> UserConfig:
    """Load a single user config from a YAML file."""
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    user = raw["user"]
    review = raw.get("review", {})
    knowledge = raw.get("knowledge", {})
    persona = raw.get("persona", {})
    tracking = raw.get("tracking", {})
    digest = raw.get("digest", {})

    return UserConfig(
        name=user["name"],
        slack_user_id=user["slack_user_id"],
        slack_user_token=_resolve_env(user.get("slack_user_token", "")),
        review_channel_id=review.get("channel_id", ""),
        github_repos=knowledge.get("github_repos", []),
        google_docs=knowledge.get("google_docs", []),
        tone=persona.get("tone", "professional but casual"),
        instructions=persona.get("instructions", ""),
        tracking_doc_id=tracking.get("google_doc_id", ""),
        digest_cron=digest.get("cron", "0 17 * * 5"),
        digest_channel=digest.get("channel", "DM"),
        exclude_dm_from=raw.get("exclude_dm_from", []),
    )


def load_all_configs(configs_dir: Path) -> dict[str, UserConfig]:
    """Load all user configs from the configs directory."""
    configs = {}
    for config_file in configs_dir.glob("*.yaml"):
        if config_file.name == "example.yaml":
            continue
        config = load_user_config(config_file)
        configs[config.slack_user_id] = config
    return configs
