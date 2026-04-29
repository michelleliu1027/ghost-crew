"""Main application: Slack event listener + review queue + digest."""

import logging
import os
from pathlib import Path

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
user_clients: dict[str, WebClient] = {}  # slack_user_id -> WebClient with user token
kb: KnowledgeBase
agent: DraftAgent
review_queue: ReviewQueue
tracker: RequestTracker
digest_store: DigestStore
scheduler: BackgroundScheduler


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

    # --- Event: someone mentions a registered user ---
    @app.event("app_mention")
    def handle_mention(event, say):
        _handle_incoming(event, bot_client)

    @app.event("message")
    def handle_message(event, say):
        # Only handle DMs (im) and group DMs (mpim)
        channel_type = event.get("channel_type")
        if channel_type not in ("im", "mpim"):
            return
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype"):
            return
        _handle_incoming(event, bot_client)

    # --- Event: reaction on review channel (approve/discard) ---
    @app.event("reaction_added")
    def handle_reaction(event):
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel = item.get("channel", "")
        message_ts = item.get("ts", "")
        reactor = event.get("user", "")

        # Check if this reaction is in a review channel for any user
        target_config = None
        for uid, cfg in configs.items():
            if cfg.review_channel_id == channel and uid == reactor:
                target_config = cfg
                break

        if not target_config:
            return

        # Fetch the review message to get metadata
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
            # Approve: send the draft as the user
            draft_text = extract_draft_from_blocks(review_msg.get("blocks", []))
            _send_as_user(owner_id, orig_channel, thread_ts, draft_text)
            digest_store.add(
                sender=reactor, channel=orig_channel,
                message=draft_text, status="approved",
            )
            # Add checkmark to review msg
            try:
                bot_client.reactions_add(channel=channel, name="robot_face", timestamp=message_ts)
            except Exception:
                pass

        elif reaction == "x":
            digest_store.add(
                sender=reactor, channel=orig_channel,
                message="(discarded)", status="discarded",
            )

    # --- Schedule weekly digest ---
    scheduler = BackgroundScheduler()

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

    # Default: every Friday 5pm
    scheduler.add_job(send_digests, "cron", day_of_week="fri", hour=17)
    scheduler.start()

    return app


def _handle_incoming(event: dict, bot_client: WebClient):
    """Process an incoming message that mentions or DMs a registered user."""
    text = event.get("text", "")
    sender = event.get("user", "")
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts")

    # Determine which registered user was mentioned or DM'd
    target_config = None

    # Check for @mentions in the text
    for uid, cfg in configs.items():
        if f"<@{uid}>" in text:
            target_config = cfg
            break

    # For DMs, check if any registered user is the recipient
    if not target_config and event.get("channel_type") in ("im", "mpim"):
        # In DMs, we need to check who the DM is with
        # The bot receives all DMs, but we route based on configured users
        for uid, cfg in configs.items():
            # For DMs, we check if the channel is a DM with this user
            # This is handled by checking membership
            try:
                members = bot_client.conversations_members(channel=channel)
                if uid in members.get("members", []):
                    target_config = cfg
                    break
            except Exception:
                continue

    if not target_config:
        return

    # Don't respond to the user's own messages
    if sender == target_config.slack_user_id:
        return

    # Get sender name
    try:
        sender_info = bot_client.users_info(user=sender)
        sender_name = sender_info["user"]["real_name"]
    except Exception:
        sender_name = sender

    # Get channel name
    try:
        channel_info = bot_client.conversations_info(channel=channel)
        channel_name = channel_info["channel"]["name"]
    except Exception:
        channel_name = channel

    # Get thread context if in a thread
    thread_context = None
    if thread_ts:
        try:
            replies = bot_client.conversations_replies(channel=channel, ts=thread_ts, limit=10)
            thread_context = [
                f"{m.get('user', 'unknown')}: {m.get('text', '')}"
                for m in replies.get("messages", [])[:-1]  # exclude the current message
            ]
        except Exception:
            pass

    # Generate draft
    logger.info(f"Generating draft for {target_config.name} — request from {sender_name}")
    try:
        draft = agent.generate_draft(
            user_config=target_config,
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
        review_channel_id=target_config.review_channel_id,
        original_channel=channel,
        original_ts=ts,
        original_thread_ts=thread_ts,
        sender_name=sender,
        original_message=text,
        draft_response=draft,
        owner_slack_id=target_config.slack_user_id,
    )

    # Track the request
    tracker.log_request(
        doc_id=target_config.tracking_doc_id,
        sender=sender_name,
        channel=channel_name,
        message=text,
        draft=draft,
    )


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
