# Ghost Crew

Your invisible AI crew that handles Slack requests on your behalf.

Ghost Crew monitors your Slack mentions and DMs, drafts responses using Claude (with context from your codebase), and lets you review before sending — all while everyone thinks they're talking to you.

## How it works

```
Someone @mentions you or DMs you on Slack
        |
        v
Ghost Crew picks it up, queries your knowledge base (GitHub repos, docs)
        |
        v
Claude drafts a response in your voice/tone
        |
        v
Draft appears in your private review channel
        |
        v
You react:  approve |  edit |  discard
        |
        v
Approved messages are sent as YOU (your Slack identity)
```

A weekly digest summarizes everything that was handled.

## Quick start

### 1. Clone and install

```bash
git clone git@github.com:your-org/ghost-crew.git
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
| `app_mentions:read` | Detect when someone @mentions you |
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels |
| `im:history` | Read DMs |
| `mpim:history` | Read group DMs |
| `chat:write` | Post drafts to your review channel |
| `reactions:read` | Detect your approve/discard reactions |
| `reactions:write` | Confirm sent messages with a reaction |
| `users:read` | Look up sender names |
| `channels:read` | Look up channel names |
| `groups:read` | Look up private channel names |
| `im:read` | Look up DM info |

**User Token Scopes** (same page, scroll down):
- `chat:write` — this is what lets Ghost Crew send messages **as you**

**Install the app** to your workspace. You'll get:
- Bot token (`xoxb-...`)
- User token (`xoxp-...`)

### 3. Set up your environment

```bash
cp .env.example .env
# Fill in your tokens:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_USER_TOKEN=xoxp-...
#   SLACK_APP_TOKEN=xapp-...
#   SLACK_SIGNING_SECRET=...
#   ANTHROPIC_API_KEY=sk-ant-...
#   GITHUB_TOKEN=ghp_...        (optional, for private repos)
```

### 4. Create your config

```bash
cp configs/example.yaml configs/your-name.yaml
```

Edit `configs/your-name.yaml`:

```yaml
user:
  name: "Your Name"
  slack_user_id: "U12345678"       # your Slack member ID
  slack_user_token: "${SLACK_USER_TOKEN}"

review:
  channel_id: "C12345678"          # your private review channel

knowledge:
  github_repos:
    - "your-org/repo-one"
    - "your-org/repo-two"

persona:
  tone: "professional but casual"
  instructions: |
    You are acting as [Your Name] from [Your Team].
    Be helpful and specific. Keep responses concise.
```

> **Find your Slack User ID:** Click your profile picture > Profile > `...` menu > Copy member ID.

### 5. Create a private review channel

Create a private Slack channel (e.g. `#yourname-ghost-crew`), invite the bot (`/invite @Ghost Crew`), and put the channel ID in your config.

### 6. Run

```bash
python -m chief_of_staff
```

## Reviewing drafts

When a request comes in, Ghost Crew posts to your review channel:

> **New request from @alice** in #marketing
> > Can you pull the conversion numbers for Q1?
>
> **Draft response:**
> Hey! Q1 conversion was 12.3%, up from 11.1% in Q4. I pulled this from the weekly_conversions dashboard...
>
> React: :white_check_mark: to send | :pencil2: to edit | :x: to discard

- **:white_check_mark:** sends the draft as you, immediately
- **:pencil2:** reply in thread with your edited version, then :white_check_mark:
- **:x:** discard, nothing is sent

## Adding more people

Ghost Crew is multi-tenant. Each person just needs:

1. Their own `configs/<name>.yaml` file
2. Their own Slack User Token (they authorize the app once)
3. Their own private review channel

Run the onboarding script:

```bash
python scripts/onboard_user.py
```

Or just copy `configs/example.yaml` and fill it in.

## Knowledge base

Ghost Crew indexes your GitHub repos into a local vector database (ChromaDB) so Claude can reference your codebase when drafting responses.

To re-index:

```bash
python scripts/reindex.py              # all users
python scripts/reindex.py "Your Name"  # specific user
```

Supported file types: `.py`, `.sql`, `.md`, `.yaml`, `.yml`, `.toml`, `.json`, `.txt`, `.sh`, `.tf`

## Optional: Google Docs tracking

If you want requests logged to a Google Doc:

1. Create a GCP service account with Google Docs API access
2. Share your tracking doc with the service account email
3. Set `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`
4. Set `tracking.google_doc_id` in your config

## Weekly digest

Every Friday at 5pm (configurable), Ghost Crew DMs you a summary:

```
Weekly Digest — 14 requests handled

 Approved: 10
 Edited before sending: 3
 Discarded: 1

Details:
 @alice in #marketing: Can you pull conversion numbers...
 @bob in #product: What's the status of the ETL pipeline...
...
```

## Architecture

```
ghost-crew/
├── configs/              # one YAML per user
├── scripts/
│   ├── onboard_user.py   # interactive setup for new users
│   └── reindex.py        # refresh knowledge base
└── src/chief_of_staff/
    ├── app.py            # Slack listener + review queue + digest scheduler
    ├── agent.py          # Claude-powered draft generation
    ├── config.py         # multi-tenant config loader
    ├── knowledge.py      # GitHub repo -> ChromaDB RAG indexer
    ├── reviewer.py       # review channel posting + approval parsing
    └── tracker.py        # Google Docs logging + digest generation
```

## License

MIT
