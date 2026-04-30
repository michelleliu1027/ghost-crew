"""Backfill: search the last N days of @mentions and generate drafts for all of them."""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from slack_sdk import WebClient

from chief_of_staff.agent import DraftAgent
from chief_of_staff.config import load_all_configs
from chief_of_staff.knowledge import KnowledgeBase
from chief_of_staff.reviewer import ReviewQueue
from chief_of_staff.tracker import RequestTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _user_already_replied(client: WebClient, user_id: str, channel_id: str, msg_ts: str, thread_ts: str | None) -> bool:
    """Check if the user has already replied in this thread."""
    try:
        root_ts = thread_ts or msg_ts
        replies = client.conversations_replies(channel=channel_id, ts=root_ts, limit=50)
        for msg in replies.get("messages", []):
            if msg.get("user") == user_id and float(msg.get("ts", 0)) > float(msg_ts):
                return True
        return False
    except Exception:
        return False


def _process_single_mention(
    match: dict,
    agent: DraftAgent,
    cfg,
    uid: str,
    bot_client: WebClient,
    user_client: WebClient,
    review_queue: ReviewQueue,
) -> dict:
    """Process a single mention: triage + generate draft. Returns result info."""
    sender = match.get("user", "") or match.get("username", "")
    text = match.get("text", "")
    ts = match.get("ts", "")
    channel_info = match.get("channel", {})
    channel_id = channel_info.get("id", "") if isinstance(channel_info, dict) else ""
    channel_name = channel_info.get("name", channel_id) if isinstance(channel_info, dict) else channel_id
    thread_ts = match.get("thread_ts")

    ts_readable = datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")

    # Get sender name
    try:
        sender_info = bot_client.users_info(user=sender)
        sender_name = sender_info["user"]["real_name"]
    except Exception:
        sender_name = sender

    # Triage
    should_reply, triage_reason = agent.triage(cfg, text, sender_name, channel_name)
    if not should_reply:
        logger.info(f"  [{ts_readable}] [{triage_reason}] {sender_name} in #{channel_name}: {text[:80]}...")
        return {"status": "skipped", "sender": sender_name, "channel": channel_name}

    logger.info(f"  [{ts_readable}] [{triage_reason}] {sender_name} in #{channel_name}: {text[:80]}...")

    # Get thread context
    thread_context = None
    if thread_ts:
        try:
            replies = user_client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=10
            )
            thread_context = [
                f"{m.get('user', 'unknown')}: {m.get('text', '')}"
                for m in replies.get("messages", [])
                if m.get("ts") != ts
            ][:5]
        except Exception:
            pass

    # Generate draft
    try:
        draft = agent.generate_draft(
            user_config=cfg,
            incoming_message=text,
            sender_name=sender_name,
            channel_name=channel_name,
            thread_context=thread_context,
        )
    except Exception as e:
        logger.error(f"  Failed to generate draft: {e}")
        return {"status": "error", "sender": sender_name, "channel": channel_name}

    # Post to review queue
    review_queue.post_draft(
        review_channel_id=cfg.review_channel_id,
        original_channel=channel_id,
        original_ts=ts,
        original_thread_ts=thread_ts,
        sender_name=sender,
        original_message=text,
        draft_response=draft,
        owner_slack_id=uid,
    )

    return {"status": "drafted", "sender": sender_name, "channel": channel_name}


def backfill(days: int = 30, target_user: str | None = None, dry_run: bool = False, workers: int = 10):
    """Search last N days of @mentions and generate drafts."""

    configs_dir = Path(os.environ.get("CONFIGS_DIR", "configs"))
    configs = load_all_configs(configs_dir)

    bot_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    kb = KnowledgeBase(persist_dir=os.environ.get("CHROMA_DIR", ".chroma"))
    agent = DraftAgent(knowledge_base=kb)
    review_queue = ReviewQueue(bot_client=bot_client)

    # Index repos
    github_token = os.environ.get("GITHUB_TOKEN")
    for uid, cfg in configs.items():
        if target_user and cfg.name.lower() != target_user.lower():
            continue
        for repo in cfg.github_repos:
            repo_url = f"https://github.com/{repo}"
            try:
                kb.index_repo(uid, repo_url, github_token=github_token)
            except Exception as e:
                logger.error(f"Failed to index {repo}: {e}")

    after_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    for uid, cfg in configs.items():
        if target_user and cfg.name.lower() != target_user.lower():
            continue

        if not cfg.slack_user_token:
            logger.warning(f"No user token for {cfg.name}, skipping")
            continue

        user_client = WebClient(token=cfg.slack_user_token)
        logger.info(f"Backfilling {days} days of @mentions for {cfg.name}...")

        # --- Phase 1: Collect all mentions + DMs ---
        all_matches = []
        seen_keys = set()
        queries = [f"<@{uid}> after:{after_date}", f"to:me after:{after_date}"]

        for query in queries:
            page = 1
            while True:
                try:
                    result = user_client.search_messages(
                        query=query,
                        sort="timestamp",
                        sort_dir="asc",
                        count=100,
                        page=page,
                    )
                except Exception as e:
                    logger.error(f"Search failed ({query}): {e}")
                    break

                matches = result.get("messages", {}).get("matches", [])
                if not matches:
                    break

                for match in matches:
                    sender = match.get("user", "") or match.get("username", "")
                    ts = match.get("ts", "")
                    channel_info = match.get("channel", {})
                    channel_id = channel_info.get("id", "") if isinstance(channel_info, dict) else ""
                    thread_ts = match.get("thread_ts")

                    # Deduplicate across queries
                    msg_key = f"{channel_id}:{ts}"
                    if msg_key in seen_keys:
                        continue
                    seen_keys.add(msg_key)

                    # Skip own messages
                    if sender == uid:
                        continue

                    # Skip bot messages
                    if match.get("bot_id") or match.get("subtype") == "bot_message":
                        continue

                    # Skip bot users
                    if sender:
                        try:
                            user_info = bot_client.users_info(user=sender)
                            if user_info.get("user", {}).get("is_bot", False):
                                continue
                        except Exception:
                            pass

                    # Skip if user already replied
                    if _user_already_replied(user_client, uid, channel_id, ts, thread_ts):
                        continue

                    all_matches.append(match)

                paging = result.get("messages", {}).get("paging", {})
                if page >= paging.get("pages", 1):
                    break
                page += 1

        logger.info(f"Found {len(all_matches)} actionable mentions + DMs (after filtering)")

        if dry_run or not all_matches:
            # In dry-run mode, still show triage results but serially (no drafts)
            for match in all_matches:
                sender = match.get("user", "") or match.get("username", "")
                text = match.get("text", "")
                ts = match.get("ts", "")
                channel_info = match.get("channel", {})
                channel_name = channel_info.get("name", "") if isinstance(channel_info, dict) else ""

                try:
                    sender_info = bot_client.users_info(user=sender)
                    sender_name = sender_info["user"]["real_name"]
                except Exception:
                    sender_name = sender

                _, triage_reason = agent.triage(cfg, text, sender_name, channel_name)
                ts_readable = datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")
                logger.info(f"  [{ts_readable}] [{triage_reason}] {sender_name} in #{channel_name}: {text[:80]}...")

            logger.info(f"Dry run complete for {cfg.name}")
            continue

        # --- Phase 2: Process in parallel ---
        logger.info(f"Processing {len(all_matches)} mentions with {workers} parallel workers...")

        total_drafted = 0
        total_skipped = 0
        total_errors = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _process_single_mention,
                    match, agent, cfg, uid, bot_client, user_client, review_queue,
                ): match
                for match in all_matches
            }

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result["status"] == "drafted":
                        total_drafted += 1
                    elif result["status"] == "skipped":
                        total_skipped += 1
                    else:
                        total_errors += 1
                except Exception as e:
                    logger.error(f"Worker failed: {e}")
                    total_errors += 1

        logger.info(
            f"Done! {cfg.name}: {total_drafted} drafted, {total_skipped} skipped, {total_errors} errors"
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backfill @mentions from the last N days")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back (default: 30)")
    parser.add_argument("--user", type=str, default=None, help="Only backfill for this user name")
    parser.add_argument("--dry-run", action="store_true", help="Just list mentions, don't generate drafts")
    parser.add_argument("--workers", type=int, default=10, help="Number of parallel workers (default: 10)")
    args = parser.parse_args()

    backfill(days=args.days, target_user=args.user, dry_run=args.dry_run, workers=args.workers)


if __name__ == "__main__":
    main()
