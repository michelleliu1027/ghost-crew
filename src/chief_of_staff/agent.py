"""Claude-powered agent that drafts responses via AWS Bedrock."""

import logging
import os

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

# Bedrock model ID
DEFAULT_MODEL = "global.anthropic.claude-opus-4-6-v1"


def _create_client() -> anthropic.Anthropic | anthropic.AnthropicBedrock:
    """Create the appropriate client based on config.

    Uses AWS Bedrock by default (via AWS SSO credentials).
    Set ANTHROPIC_API_KEY to use the Anthropic API directly instead.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("Using Anthropic API directly")
        return anthropic.Anthropic()
    else:
        region = os.environ.get("AWS_REGION", "us-east-1")
        logger.info(f"Using AWS Bedrock in {region}")
        return anthropic.AnthropicBedrock(aws_region=region)


TRIAGE_PROMPT = """You are a message triage assistant for {name}.

Decide if this Slack message requires a work-related response from {name}.

REPLY — messages that need a response:
- Work requests, questions about data/code/dashboards
- Action items or asks directed at {name}
- Cross-team requests (marketing, product, etc.)
- Meeting follow-ups or project updates that need input

SKIP — messages that do NOT need a response:
- Casual/social chat from friends (lunch plans, jokes, memes, banter)
- Messages where {name} is just tagged for visibility but no action needed
- Group announcements that don't require a personal reply
- Someone saying "thanks" or "got it" with no follow-up needed
- Messages in clearly social/fun channels (hackweek challenges, emoji channels, etc.)

Respond with ONLY one word: REPLY or SKIP"""


class DraftAgent:
    def __init__(self, knowledge_base: KnowledgeBase):
        self.client = _create_client()
        self.kb = knowledge_base
        self.model = os.environ.get("MODEL_ID", DEFAULT_MODEL)
        self.triage_model = os.environ.get("TRIAGE_MODEL_ID", "global.anthropic.claude-sonnet-4-6-v1")

    def triage(
        self,
        user_config: UserConfig,
        incoming_message: str,
        sender_name: str,
        channel_name: str,
    ) -> bool:
        """Return True if the message is worth drafting a reply for."""
        system_prompt = TRIAGE_PROMPT.format(name=user_config.name)
        user_message = f"Sender: {sender_name}\nChannel: #{channel_name}\nMessage: {incoming_message}"

        try:
            response = self.client.messages.create(
                model=self.triage_model,
                max_tokens=10,
                timeout=30,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            answer = response.content[0].text.strip().upper()
            return answer == "REPLY"
        except Exception as e:
            logger.error(f"Triage failed: {e}")
            return True  # if triage fails, default to drafting

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
            model=self.model,
            max_tokens=1024,
            timeout=120,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return response.content[0].text
