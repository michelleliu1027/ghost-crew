"""Interactive script to onboard a new user to AI Chief of Staff."""

import sys
from pathlib import Path

import yaml


def main():
    print("=== AI Chief of Staff — New User Onboarding ===\n")

    name = input("Your name: ").strip()
    slack_user_id = input("Your Slack user ID (e.g. U12345678): ").strip()
    slack_user_token = input("Your Slack user token (xoxp-...): ").strip()
    review_channel = input("Review channel ID (private channel for drafts): ").strip()

    repos = []
    print("\nGitHub repos for knowledge base (empty line to stop):")
    while True:
        repo = input("  Repo (e.g. Kikoff/dagster-etl): ").strip()
        if not repo:
            break
        repos.append(repo)

    tone = input("\nDescribe your tone (e.g. 'professional but casual'): ").strip() or "professional but casual"
    instructions = input("Any special instructions for the agent? ").strip()

    tracking_doc = input("\nGoogle Doc ID for tracking (optional): ").strip()

    config = {
        "user": {
            "name": name,
            "slack_user_id": slack_user_id,
            "slack_user_token": slack_user_token,
        },
        "review": {
            "channel_id": review_channel,
        },
        "knowledge": {
            "github_repos": repos,
        },
        "persona": {
            "tone": tone,
            "instructions": instructions,
        },
        "tracking": {
            "google_doc_id": tracking_doc,
        },
        "digest": {
            "cron": "0 17 * * 5",
            "channel": "DM",
        },
    }

    filename = name.lower().replace(" ", "-") + ".yaml"
    config_path = Path("configs") / filename

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\nConfig saved to {config_path}")
    print("Restart the app to pick up the new config.")


if __name__ == "__main__":
    main()
