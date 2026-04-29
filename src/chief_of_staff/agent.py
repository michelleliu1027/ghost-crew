"""Claude-powered agent that drafts responses."""

import logging

import anthropic

from .config import UserConfig
from .knowledge import KnowledgeBase

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are an AI assistant acting on behalf of {name}.
You are drafting a response that {name} will review before sending.

{instructions}

Tone: {tone}

Important rules:
- Write as if you ARE {name} — use first person ("I", "my", "we" for the team)
- Be helpful and specific
- Keep responses concise but thorough
- If you're unsure, say so — {name} will edit before sending
- Never reveal that you are an AI or that this is a draft
"""


class DraftAgent:
    def __init__(self, knowledge_base: KnowledgeBase):
        self.client = anthropic.Anthropic()
        self.kb = knowledge_base

    def generate_draft(
        self,
        user_config: UserConfig,
        incoming_message: str,
        sender_name: str,
        channel_name: str,
        thread_context: list[str] | None = None,
    ) -> str:
        """Generate a draft response for the user to review."""
        # Retrieve relevant context from knowledge base
        context_docs = self.kb.query(user_config.slack_user_id, incoming_message)
        context_text = ""
        if context_docs:
            context_text = "\n\nRelevant context from codebase:\n"
            for doc in context_docs:
                context_text += f"\n---\n{doc['path']} ({doc['repo']})\n{doc['content']}\n"

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            name=user_config.name,
            instructions=user_config.instructions,
            tone=user_config.tone,
        )

        # Build message with context
        user_message = f"""Someone sent a message that needs a response.

Sender: {sender_name}
Channel: #{channel_name}
Message: {incoming_message}
"""
        if thread_context:
            user_message += "\nThread context (previous messages):\n"
            for msg in thread_context[-5:]:  # last 5 messages for context
                user_message += f"- {msg}\n"

        if context_text:
            user_message += context_text

        user_message += "\nDraft a response:"

        response = self.client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return response.content[0].text
