"""Google Docs request tracker and weekly digest generator."""

import logging
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


class RequestTracker:
    def __init__(self, service_account_json: str | None = None):
        self._docs_service = None
        self._sa_json = service_account_json

    @property
    def docs_service(self):
        if self._docs_service is None and self._sa_json:
            creds = service_account.Credentials.from_service_account_file(
                self._sa_json,
                scopes=["https://www.googleapis.com/auth/documents"],
            )
            self._docs_service = build("docs", "v1", credentials=creds)
        return self._docs_service

    def log_request(
        self,
        doc_id: str,
        sender: str,
        channel: str,
        message: str,
        draft: str,
        status: str = "pending",
    ):
        """Append a request entry to the tracking Google Doc."""
        if not self.docs_service or not doc_id:
            logger.warning("Google Docs not configured, skipping tracking")
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = (
            f"\n---\n"
            f"Date: {now}\n"
            f"From: {sender}\n"
            f"Channel: #{channel}\n"
            f"Request: {message[:500]}\n"
            f"Status: {status}\n"
        )

        try:
            self.docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "location": {"index": 1},
                                "text": entry,
                            }
                        }
                    ]
                },
            ).execute()
            logger.info(f"Logged request from {sender} to doc {doc_id}")
        except Exception as e:
            logger.error(f"Failed to log request to Google Doc: {e}")


class DigestStore:
    """Simple in-memory store for tracking handled requests for digest."""

    def __init__(self):
        self.entries: list[dict] = []

    def add(self, sender: str, channel: str, message: str, status: str, responded_at: str = ""):
        self.entries.append({
            "sender": sender,
            "channel": channel,
            "message": message[:200],
            "status": status,
            "responded_at": responded_at,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def flush(self) -> list[dict]:
        """Return all entries and clear the store."""
        entries = self.entries.copy()
        self.entries.clear()
        return entries

    def generate_digest_text(self) -> str:
        """Generate a human-readable weekly digest."""
        entries = self.flush()
        if not entries:
            return "No requests handled this period."

        approved = [e for e in entries if e["status"] == "approved"]
        discarded = [e for e in entries if e["status"] == "discarded"]
        edited = [e for e in entries if e["status"] == "edited"]

        lines = [
            f"*Weekly Digest* — {len(entries)} requests handled\n",
            f":white_check_mark: Auto-approved: {len(approved)}",
            f":pencil2: Edited before sending: {len(edited)}",
            f":x: Discarded: {len(discarded)}\n",
            "*Details:*",
        ]

        for e in entries:
            emoji = {"approved": ":white_check_mark:", "edited": ":pencil2:", "discarded": ":x:"}.get(
                e["status"], ":grey_question:"
            )
            lines.append(f"{emoji} <@{e['sender']}> in #{e['channel']}: {e['message']}")

        return "\n".join(lines)
