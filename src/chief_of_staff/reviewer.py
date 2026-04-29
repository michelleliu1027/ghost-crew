"""Review queue: post drafts to a private Slack channel for approval."""

import json
import logging

from slack_sdk import WebClient

logger = logging.getLogger(__name__)


class ReviewQueue:
    def __init__(self, bot_client: WebClient):
        self.bot_client = bot_client

    def post_draft(
        self,
        review_channel_id: str,
        original_channel: str,
        original_ts: str,
        original_thread_ts: str | None,
        sender_name: str,
        original_message: str,
        draft_response: str,
        owner_slack_id: str,
    ) -> str | None:
        """Post a draft to the review channel. Returns the review message ts."""
        # Metadata for routing the approved response back
        metadata = {
            "channel": original_channel,
            "ts": original_ts,
            "thread_ts": original_thread_ts or original_ts,
            "owner": owner_slack_id,
        }

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New request from <@{sender_name}>* in <#{original_channel}>",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f">>> {original_message[:1500]}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Draft response:*\n{draft_response}",
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "React: :white_check_mark: to send | :pencil2: to edit (reply with edit in thread) | :x: to discard",
                    }
                ],
            },
        ]

        try:
            result = self.bot_client.chat_postMessage(
                channel=review_channel_id,
                text=f"Draft for request from {sender_name}",
                blocks=blocks,
                metadata={
                    "event_type": "draft_review",
                    "event_payload": metadata,
                },
            )
            return result["ts"]
        except Exception as e:
            logger.error(f"Failed to post draft to review channel: {e}")
            return None


def parse_review_metadata(message: dict) -> dict | None:
    """Extract routing metadata from a review message."""
    metadata = message.get("metadata", {})
    if metadata.get("event_type") == "draft_review":
        return metadata.get("event_payload", {})
    return None


def extract_draft_from_blocks(blocks: list) -> str:
    """Extract the draft response text from review message blocks."""
    for i, block in enumerate(blocks):
        text = block.get("text", {}).get("text", "")
        if text.startswith("*Draft response:*"):
            return text.replace("*Draft response:*\n", "")
    return ""
