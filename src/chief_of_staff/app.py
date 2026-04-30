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
        """Search for recent @mentions and DMs of each registered user."""
        for uid, cfg in configs.items():
            client = user_clients.get(uid)
            if not client:
                continue

            # Run two searches: @mentions across all channels + DMs
            all_matches = []
            for query in [f"<@{uid}>", "to:me"]:
                try:
                    result = client.search_messages(
                        query=query,
                        sort="timestamp",
                        sort_dir="desc",
                        count=20,
                    )
                    all_matches.extend(result.get("messages", {}).get("matches", []))
                except Exception as e:
                    logger.error(f"Failed to search ({query}) for {cfg.name}: {e}")

            matches = all_matches
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
    worker_id: int = 0,
) -> dict:
    """Process a single mention: triage → draft. Returns result dict (doesn't post yet)."""
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

    logger.info(f"[Agent #{worker_id}] Processing: {sender_name} in #{channel_name}")

    # Skip DMs from excluded users
    is_dm = channel_id.startswith("D")
    if is_dm and sender in cfg.exclude_dm_from:
        logger.info(f"[Agent #{worker_id}] Skipped DM from excluded user: {sender_name}")
        return {"status": "skipped", "reason": "SKIP: excluded DM contact", "sender": sender,
                "sender_name": sender_name, "channel_id": channel_id, "channel_name": channel_name,
                "text": text, "msg_link": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"}

    msg_ts_link = ts.replace(".", "")
    msg_link = f"https://slack.com/archives/{channel_id}/p{msg_ts_link}"

    # Triage
    should_reply, triage_reason = agent.triage(cfg, text, sender_name, channel_name)
    if not should_reply:
        logger.info(f"[Agent #{worker_id}] {triage_reason} — {sender_name}")
        return {
            "status": "skipped",
            "reason": triage_reason,
            "sender": sender,
            "sender_name": sender_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "text": text,
            "msg_link": msg_link,
        }

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
    logger.info(f"[Agent #{worker_id}] Drafting response for {sender_name}...")
    try:
        draft = agent.generate_draft(
            user_config=cfg,
            incoming_message=text,
            sender_name=sender_name,
            channel_name=channel_name,
            thread_context=thread_context,
        )
    except Exception as e:
        logger.error(f"[Agent #{worker_id}] Failed to generate draft: {e}")
        return {"status": "error", "sender_name": sender_name}

    logger.info(f"[Agent #{worker_id}] Done: draft ready for {sender_name}")
    return {
        "status": "drafted",
        "match": match,
        "sender": sender,
        "sender_name": sender_name,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "text": text,
        "ts": ts,
        "thread_ts": thread_ts,
        "draft": draft,
        "msg_link": msg_link,
    }


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
    """Process mentions in parallel, then post grouped: drafts first, skips together."""
    n = len(matches)
    actual_workers = min(max_workers, n)
    logger.info(f"Dispatching {n} mentions to {actual_workers} agents...")

    try:
        bot_client.chat_postMessage(
            channel=cfg.review_channel_id,
            text=f":rocket: *Ghost Crew dispatched {actual_workers} agents* to process {n} mentions...",
        )
    except Exception:
        pass

    import time as _time
    start = _time.time()

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_single_mention,
                match, cfg, uid, bot_client, user_client, agent, review_queue, tracker,
                worker_id=i + 1,
            ): match
            for i, match in enumerate(matches)
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Worker failed: {e}")
                results.append({"status": "error"})

    # --- Post to review channel: drafts first, then skips ---
    drafted_results = [r for r in results if r["status"] == "drafted"]
    skipped_results = [r for r in results if r["status"] == "skipped"]
    error_count = sum(1 for r in results if r["status"] == "error")

    # 1. Post each draft individually (needs separate approval)
    for r in drafted_results:
        review_queue.post_draft(
            review_channel_id=cfg.review_channel_id,
            original_channel=r["channel_id"],
            original_ts=r["ts"],
            original_thread_ts=r["thread_ts"],
            sender_name=r["sender"],
            original_message=r["text"],
            draft_response=r["draft"],
            owner_slack_id=uid,
        )
        tracker.log_request(
            doc_id=cfg.tracking_doc_id,
            sender=r["sender_name"],
            channel=r["channel_name"],
            message=r["text"],
            draft=r["draft"],
        )

    # 2. Post all skips as one consolidated message
    if skipped_results:
        skip_lines = []
        for r in skipped_results:
            skip_lines.append(
                f"• <@{r['sender']}> in <#{r['channel_id']}> — _{r['reason']}_ | <{r['msg_link']}|View>"
            )
        skip_text = ":see_no_evil: *Skipped messages:*\n" + "\n".join(skip_lines)
        try:
            bot_client.chat_postMessage(
                channel=cfg.review_channel_id,
                text=skip_text,
            )
        except Exception:
            pass

    # 3. Summary
    elapsed = round(_time.time() - start, 1)
    summary = (
        f":checkered_flag: *Ghost Crew batch complete* — {elapsed}s\n"
        f":white_check_mark: {len(drafted_results)} drafted | :see_no_evil: {len(skipped_results)} skipped | :warning: {error_count} errors"
    )
    logger.info(summary)

    try:
        bot_client.chat_postMessage(
            channel=cfg.review_channel_id,
            text=summary,
        )
    except Exception:
        pass


def _user_already_replied(client: WebClient, user_id: str, channel_id: str, msg_ts: str, thread_ts: str | None) -> bool:
    """Check if the user has already replied in this thread or DM conversation."""
    # 1. Check thread replies (for threaded messages)
    if thread_ts:
        try:
            replies = client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=50
            )
            for msg in replies.get("messages", []):
                if msg.get("user") == user_id and float(msg.get("ts", 0)) > float(msg_ts):
                    return True
        except Exception:
            pass

    # 2. Check channel history after the message (for flat DMs and non-threaded messages)
    try:
        history = client.conversations_history(
            channel=channel_id, oldest=msg_ts, limit=20
        )
        for msg in history.get("messages", []):
            if msg.get("user") == user_id and float(msg.get("ts", 0)) > float(msg_ts):
                return True
    except Exception:
        pass

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
