"""Re-index knowledge base for all users (or a specific user)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from chief_of_staff.config import load_all_configs
from chief_of_staff.knowledge import KnowledgeBase


def main():
    configs_dir = Path(os.environ.get("CONFIGS_DIR", "configs"))
    configs = load_all_configs(configs_dir)
    github_token = os.environ.get("GITHUB_TOKEN")

    target_user = sys.argv[1] if len(sys.argv) > 1 else None

    kb = KnowledgeBase(persist_dir=os.environ.get("CHROMA_DIR", ".chroma"))

    for uid, cfg in configs.items():
        if target_user and cfg.name.lower() != target_user.lower():
            continue

        print(f"Indexing repos for {cfg.name}...")
        for repo in cfg.github_repos:
            repo_url = f"https://github.com/{repo}"
            print(f"  {repo_url}")
            kb.index_repo(uid, repo_url, github_token=github_token)

    print("Done.")


if __name__ == "__main__":
    main()
