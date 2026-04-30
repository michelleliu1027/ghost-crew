"""Main application: poll for @mentions + review queue + digest."""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .agent import DraftAgent
from .config import UserConfig, load_all_configs
from .knowledge import KnowledgeBase
from .reviewer import ReviewQueue, extract_draft_from_blocks, parse_review_metadata
from .tracker import DigestStore, RequestTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global state ---
configs: dict[str, UserConfig] = {}
user_clients: dict[str, WebClient] = {}
kb: KnowledgeBase
agent: DraftAgent
review_queue: ReviewQueue
tracker: RequestTracker
digest_store: DigestStore
scheduler: BackgroundScheduler
seen_messages: set[str] = set()  # track already-processed message timestamps


def create_app() -> App:
    global configs, user_clients, kb, agent, review_queue, tracker, digest_store, scheduler

    bot_token = os.environ["SLACK_BOT_TOKEN"]
    app = App(token=bot_token)
    bot_client = app.client

    # Load user configs
    configs_dir = Path(os.environ.get("CONFIGS_DIR", "configs"))
    configs = load_all_configs(configs_dir)
    logger.info(f"Loaded configs for {len(configs)} users: {[c.name for c in configs.values()]}")

    # Initialize user-specific Slack clients (for sending as the user)
    for uid, cfg in configs.items():
        if cfg.slack_user_token:
            user_clients[uid] = WebClient(token=cfg.slack_user_token)

    # Initialize components
    kb = KnowledgeBase(persist_dir=os.environ.get("CHROMA_DIR", ".chroma"))
    agent = DraftAgent(knowledge_base=kb)
    review_queue = ReviewQueue(bot_client=bot_client)
    tracker = RequestTracker(
        service_account_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    digest_store = DigestStore()

    # --- Index repos on startup ---
    github_token = os.environ.get("GITHUB_TOKEN")
    for uid, cfg in configs.items():
        for repo in cfg.github_repos:
            repo_url = f"https://github.com/{repo}"
            try:
                kb.index_repo(uid, repo_url, github_token=github_token)
            except Exception as e:
                logger.error(f"Failed to index {repo} for {cfg.name}: {e}")

    # --- Event: reaction on review channel (approve/discard) ---
    @app.event("app_mention")
    def handle_mention(event, say):
        pass  # handled by polling instead

    @app.event("message")
    def handle_message(event, say):
        pass  # handled by polling instead

    @app.event("reaction_added")
    def handle_reaction(event):
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel = item.get("channel", "")
        message_ts = item.get("ts", "")
        reactor = event.get("user", "")

        target_config = None
        for uid, cfg in configs.items():
            if cfg.review_channel_id == channel and uid == reactor:
                target_config = cfg
                break

        if not target_config:
            return

        try:
            result = bot_client.conversations_history(
                channel=channel, latest=message_ts, inclusive=True, limit=1
            )
            if not result["messages"]:
                return
            review_msg = result["messages"][0]
        except Exception as e:
            logger.error(f"Failed to fetch review message: {e}")
            return

        metadata = parse_review_metadata(review_msg)
        if not metadata:
            return

        orig_channel = metadata["channel"]
        thread_ts = metadata["thread_ts"]
        owner_id = metadata["owner"]

        if reaction == "white_check_mark":
            draft_text = extract_draft_from_blocks(review_msg.get("blocks", []))
            _send_as_user(owner_id, orig_channel, thread_ts, draft_text)
            digest_store.add(
                sender=reactor, channel=orig_channel,
                message=draft_text, status="approved",
            )
            try:
                bot_client.reactions_add(channel=channel, name="robot_face", timestamp=message_ts)
            except Exception:
                pass

        elif reaction == "x":
            digest_store.add(
                sender=reactor, channel=orig_channel,
                message="(discarded)", status="discarded",
            )

    # --- Polling: search for @mentions using user token ---
    scheduler = BackgroundScheduler()

    def poll_mentions():
        """Search for recent @mentions of each registered user."""
        for uid, cfg in configs.items():
            client = user_clients.get(uid)
            if not client:
                continue

            try:
                # Search for messages mentioning this user in the last 2 minutes
                result = client.search_messages(
                    query=f"<@{uid}>",
                    sort="timestamp",
                    sort_dir="desc",
                    count=20,
                )
            except Exception as e:
                logger.error(f"Failed to search mentions for {cfg.name}: {e}")
                continue

            matches = result.get("messages", {}).get("matches", [])
            pending = []
            for match in matches:
                ts = match.get("ts", "")
                channel_info = match.get("channel", {})
                channel_id = channel_info.get("id", "") if isinstance(channel_info, dict) else ""
                sender = match.get("user", "") or match.get("username", "")
                text = match.get("text", "")

                # Skip if already processed
                msg_key = f"{channel_id}:{ts}"
                if msg_key in seen_messages:
                    continue
                seen_messages.add(msg_key)

                # Skip own messages
                if sender == uid:
                    continue

                # Skip messages from bots
                if match.get("bot_id") or match.get("subtype") == "bot_message":
                    continue

                # Skip bot users (check via users.info)
                if sender:
                    try:
                        user_info = bot_client.users_info(user=sender)
                        if user_info.get("user", {}).get("is_bot", False):
                            continue
                    except Exception:
                        pass

                # Skip if user already replied in this thread
                if _user_already_replied(client, uid, channel_id, ts, match.get("thread_ts")):
                    continue

                pending.append(match)

            # Process pending mentions in parallel
            if pending:
                _process_mentions_parallel(
                    pending, cfg, uid, bot_client, client, agent,
                    review_queue, tracker,
                )

        # Prevent seen_messages from growing forever
        if len(seen_messages) > 10000:
            seen_messages.clear()

    # Poll every 30 seconds
    poll_interval = int(os.environ.get("POLL_INTERVAL", "30"))
    scheduler.add_job(poll_mentions, "interval", seconds=poll_interval, id="poll_mentions")

    # Weekly digest
    def send_digests():
        for uid, cfg in configs.items():
            digest_text = digest_store.generate_digest_text()
            if not digest_text:
                continue
            try:
                if cfg.digest_channel == "DM":
                    bot_client.chat_postMessage(channel=uid, text=digest_text)
                else:
                    bot_client.chat_postMessage(channel=cfg.digest_channel, text=digest_text)
                logger.info(f"Sent digest to {cfg.name}")
            except Exception as e:
                logger.error(f"Failed to send digest to {cfg.name}: {e}")

    scheduler.add_job(send_digests, "cron", day_of_week="fri", hour=17)
    scheduler.start()

    # Run initial poll immediately
    poll_mentions()

    return app


def _process_single_mention(
    match: dict,
    cfg,
    uid: str,
    bot_client: WebClient,
    user_client: WebClient,
    agent: DraftAgent,
    review_queue: ReviewQueue,
    tracker: RequestTracker,
):
    """Process a single mention: triage → draft → post to review."""
    sender = match.get("user", "") or match.get("username", "")
    text = match.get("text", "")
    ts = match.get("ts", "")
    channel_info = match.get("channel", {})
    channel_id = channel_info.get("id", "") if isinstance(channel_info, dict) else ""
    channel_name = channel_info.get("name", channel_id) if isinstance(channel_info, dict) else channel_id
    thread_ts = match.get("thread_ts")

    try:
        sender_info = bot_client.users_info(user=sender)
        sender_name = sender_info["user"]["real_name"]
    except Exception:
        sender_name = sender

    logger.info(f"Processing mention from {sender_name} in #{channel_name}")

    # Triage
    if not agent.triage(cfg, text, sender_name, channel_name):
        logger.info(f"  Skipped (triage: not worth replying)")
        msg_ts_link = ts.replace(".", "")
        msg_link = f"https://slack.com/archives/{channel_id}/p{msg_ts_link}"
        try:
            bot_client.chat_postMessage(
                channel=cfg.review_channel_id,
                text=f":see_no_evil: Skipped — <@{sender}> in <#{channel_id}> | <{msg_link}|View>\n>>> {text[:300]}",
            )
        except Exception:
            pass
        return

    # Get thread context
    thread_context = None
    if thread_ts:
        try:
            replies = user_client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=10
            )
            thread_context = [
                f"{m.get('user', 'unknown')}: {m.get('text', '')}"
                for m in replies.get("messages", [])[:-1]
            ]
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
        logger.error(f"Failed to generate draft: {e}")
        return

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

    # Track
    tracker.log_request(
        doc_id=cfg.tracking_doc_id,
        sender=sender_name,
        channel=channel_name,
        message=text,
        draft=draft,
    )


def _process_mentions_parallel(
    matches: list,
    cfg,
    uid: str,
    bot_client: WebClient,
    user_client: WebClient,
    agent: DraftAgent,
    review_queue: ReviewQueue,
    tracker: RequestTracker,
    max_workers: int = 10,
):
    """Process multiple mentions in parallel."""
    logger.info(f"Processing {len(matches)} mentions in parallel (max {max_workers} workers)")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_single_mention,
                match, cfg, uid, bot_client, user_client, agent, review_queue, tracker,
            ): match
            for match in matches
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Worker failed: {e}")


def _user_already_replied(client: WebClient, user_id: str, channel_id: str, msg_ts: str, thread_ts: str | None) -> bool:
    """Check if the user has already replied in this thread/conversation."""
    try:
        # If the message is in a thread, check thread replies
        root_ts = thread_ts or msg_ts
        replies = client.conversations_replies(
            channel=channel_id, ts=root_ts, limit=50
        )
        messages = replies.get("messages", [])
        # Check if any reply after the mention is from the user
        for msg in messages:
            if msg.get("user") == user_id and msg.get("ts") != root_ts:
                # User replied in this thread
                return True
            if msg.get("user") == user_id and float(msg.get("ts", 0)) > float(msg_ts):
                return True
        return False
    except Exception:
        # If we can't check, don't skip (better to draft than miss)
        return False


def _send_as_user(user_id: str, channel: str, thread_ts: str, text: str):
    """Send a message as the user (using their user token)."""
    client = user_clients.get(user_id)
    if not client:
        logger.error(f"No user client for {user_id}, falling back to bot")
        return

    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
        )
        logger.info(f"Sent response as {user_id} in {channel}")
    except Exception as e:
        logger.error(f"Failed to send as user {user_id}: {e}")


def main():
    app = create_app()
    app_token = os.environ["SLACK_APP_TOKEN"]
    handler = SocketModeHandler(app, app_token)
    logger.info("Ghost Crew is running!")
    handler.start()


if __name__ == "__main__":
    main()
