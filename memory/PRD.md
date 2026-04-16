# Transport Monitor Bot - PRD

## Architecture
- **Bot**: Python, running inside FastAPI process in **WEBHOOK mode**
- **Backend**: FastAPI at `/app/backend/server.py` + `/app/backend/telegram_bot.py`
- **Frontend**: React dashboard at `/app/frontend/src/App.js`
- **Storage**: JSON files (config + state)
- **Webhook URL**: `POST /api/bot/webhook/<secret>` — Telegram pushes updates here

## How Webhook Mode Works
1. On startup, backend calls `setWebhook` with our external URL + secret token
2. Telegram POSTs each new message to our webhook endpoint
3. No background polling task — fully event-driven
4. Pod suspension is OK: incoming webhook wakes the pod
5. On shutdown, `deleteWebhook` is called to clean up

## What's Been Implemented
- [x] Webhook mode (no polling, Telegram pushes to us)
- [x] Webhook secret derived from bot token (no extra env vars needed)
- [x] Secret validation on incoming webhooks (403 for invalid)
- [x] Full bot logic: 44 keyword filtering, 8 negative keywords, multi-recipient forwarding
- [x] Telegram commands: /start, /status, /threshold, /llm, /debug, /addchat, /rmchat
- [x] Web dashboard with live health metrics
- [x] Truthful health model: webhook_count, last_webhook_received, last_error
- [x] Clean restart: re-registers webhook
- [x] Tokens in env vars only (not in committed files)

## Platform Limitation — RESOLVED
Pod suspension was the root cause of "bot sleeping". Webhook mode solves this:
- Long-polling required persistent process → pod sleeps → bot dies
- Webhook mode: Telegram sends HTTP POST → pod wakes up → processes message

## Files
- `backend/server.py` — FastAPI server with webhook endpoint
- `backend/telegram_bot.py` — Bot logic (webhook handler, commands, filtering)
- `backend/.env` — TELEGRAM_BOT_TOKEN, EMERGENT_LLM_KEY
- `bot/transport-bot-config.json` — Keywords, thresholds (no secrets)
- `bot/transport-bot-state.json` — Last update ID, seen messages
- `frontend/src/App.js` — Dashboard

## Backlog
- [ ] LLM analysis via /llm on (Claude Sonnet 4.5)
- [ ] Web UI keyword editor
- [ ] Statistics history charts
