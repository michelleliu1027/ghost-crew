# Ghost Crew

Your invisible AI crew that handles Slack requests on your behalf.

People @mention you on Slack. Ghost Crew picks it up, drafts a response using Claude (with context from your codebase), and sends it to your private review channel. You approve, edit, or discard — and the response goes out as you. Nobody knows.

## Why

If you're the kind of person who gets @mentioned 30 times a day across random Slack channels — data requests, status updates, "hey can you check this" — and you end up asking AI to draft most of your replies anyway, this automates that entire loop.

- **Fully invisible** — no bot joins your channels, no one sees anything
- **You stay in control** — every draft goes through your review before sending
- **Knows your codebase** — indexes your GitHub repos so Claude gives accurate, context-aware answers
- **Smart filtering** — only drafts replies for messages you haven't responded to yet
- **Multi-tenant** — your whole team can use it, each with their own config
- **Backfill** — catch up on the last 30 days of unanswered @mentions in one go

## How it works

```
Someone @mentions you on Slack (any channel, any DM)
        |
        v
Ghost Crew polls for new mentions every 30s (using your user token)
        |
        v
Checks: have you already replied? If yes, skip.
        |
        v
Queries your knowledge base (GitHub repos, docs) for relevant context
        |
        v
Claude drafts a response in your voice
        |
        v
Draft appears in your private review channel
        |
        v
You react:  approve  |  edit  |  discard
        |
        v
Approved messages are sent as YOU (your Slack identity)
```

## Quick start

### 1. Clone and install

```bash
git clone git@github.com:michelleliu1027/ghost-crew.git
cd ghost-crew
pip install -e .
```

### 2. Create a Slack App

Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app from scratch.

**Enable Socket Mode:**
- Sidebar > Socket Mode > Enable
- Create an App-Level Token with `connections:write` scope
- Save the `xapp-` token

**Subscribe to events** (Sidebar > Event Subscriptions > Enable):
- `app_mention`
- `message.im`
- `message.mpim`
- `reaction_added`

**Bot Token Scopes** (Sidebar > OAuth & Permissions):

| Scope | Why |
|-------|-----|
| `chat:write` | Post drafts to your review channel |
| `reactions:read` | Detect your approve/discard reactions |
| `reactions:write` | Confirm sent messages with a reaction |
| `users:read` | Look up sender names |
| `channels:read` | Look up channel names |
| `groups:read` | Look up private channel names |

**User Token Scopes** (same page, scroll down):

| Scope | Why |
|-------|-----|
| `chat:write` | Send messages as you |
| `search:read` | Search for @mentions across all channels |
| `search:read.im` | Search in DMs |
| `search:read.mpim` | Search in group DMs |
| `search:read.private` | Search in private channels |
| `search:read.public` | Search in public channels |

**Install the app** to your workspace. You'll get:
- Bot token (`xoxb-...`)
- User token (`xoxp-...`)

### 3. Set up your environment

```bash
cp .env.example .env
```

Fill in your `.env`:
```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_TOKEN=xoxp-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# Claude — pick one:
# Option A: AWS Bedrock (uses AWS SSO, no API key needed)
AWS_REGION=us-east-1
# Option B: Anthropic API directly
# ANTHROPIC_API_KEY=sk-ant-...

# Optional
GITHUB_TOKEN=ghp_...
```

### 4. Create your config

```bash
cp configs/example.yaml configs/your-name.yaml
```

```yaml
user:
  name: "Your Name"
  slack_user_id: "U12345678"       # Profile > ... > Copy member ID
  slack_user_token: "${SLACK_USER_TOKEN}"

review:
  channel_id: "C12345678"          # your private review channel

knowledge:
  github_repos:
    - "your-org/your-repo"

persona:
  tone: "professional but casual"
  instructions: |
    You are acting as [Your Name] from [Your Team].
    Be helpful and specific. Keep responses concise.

digest:
  cron: "0 17 * * 5"
  channel: "DM"
```

### 5. Create a private review channel

Create a private Slack channel (e.g. `#yourname-ghost-crew`), invite the bot (`/invite @Ghost Crew`), and put the channel ID in your config.

### 6. Run

```bash
python -m chief_of_staff
```

To run in the background:
```bash
nohup python -m chief_of_staff > ghost-crew.log 2>&1 &
```

## Backfill

First time using Ghost Crew? Catch up on unanswered @mentions from the last 30 days:

```bash
# Preview what it finds (no drafts generated)
python scripts/backfill.py --dry-run

# Generate drafts for all unanswered mentions
python scripts/backfill.py

# Customize
python scripts/backfill.py --days 7
python scripts/backfill.py --user "Your Name" --days 14
```

Only messages you **haven't already replied to** will get drafts — so it won't spam your review channel with stuff you've already handled.

## Reviewing drafts

When a request comes in, Ghost Crew posts to your review channel:

> **New request from @alice** in #marketing | [View original](https://slack.com/archives/C123/p1234567890)
> > Can you pull the conversion numbers for Q1?
>
> **Draft response:**
> Hey! Q1 conversion was 12.3%, up from 11.1% in Q4. I pulled this from the weekly_conversions dashboard...
>
> React: :white_check_mark: to send | :pencil2: to edit | :x: to discard

Each draft includes a **direct link to the original message** so you can jump to the full context with one click.

- :white_check_mark: — sends the draft as you, immediately
- :pencil2: — reply in thread with your edited version, then :white_check_mark:
- :x: — discard, nothing is sent

## Adding more people

Ghost Crew is multi-tenant. Each person just needs:

1. Their own `configs/<name>.yaml` file
2. Their own Slack User Token (they authorize the app once)
3. Their own private review channel

```bash
python scripts/onboard_user.py
```

## Knowledge base

Ghost Crew indexes your GitHub repos into a local vector database (ChromaDB) so Claude can reference your actual code when drafting responses.

```bash
python scripts/reindex.py              # all users
python scripts/reindex.py "Your Name"  # specific user
```

Supported: `.py` `.sql` `.md` `.yaml` `.yml` `.toml` `.json` `.txt` `.sh` `.tf`

## Weekly digest

Every Friday at 5pm (configurable), you get a summary:

```
Weekly Digest — 14 requests handled

  Approved: 10
  Edited before sending: 3
  Discarded: 1

Details:
  @alice in #marketing: Can you pull conversion numbers...
  @bob in #product: What's the status of the ETL pipeline...
```

## Architecture

```
ghost-crew/
├── configs/              # one YAML per user
├── scripts/
│   ├── backfill.py       # catch up on last N days of @mentions
│   ├── onboard_user.py   # interactive setup for new users
│   └── reindex.py        # refresh knowledge base
└── src/chief_of_staff/
    ├── app.py            # mention polling + review queue + digest scheduler
    ├── agent.py          # Claude draft generation (Bedrock or Anthropic API)
    ├── config.py         # multi-tenant config loader
    ├── knowledge.py      # GitHub repo -> ChromaDB RAG indexer
    ├── reviewer.py       # review channel posting + approval parsing
    └── tracker.py        # Google Docs logging + digest generation
```

## License

MIT
