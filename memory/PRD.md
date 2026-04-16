# Transport Monitor Bot - PRD

## Architecture
- **Bot**: Python asyncio task running INSIDE FastAPI backend (no Node.js subprocess)
- **Backend**: FastAPI (Python) at `/app/backend/server.py` + `/app/backend/telegram_bot.py`
- **Frontend**: React dashboard at `/app/frontend/src/App.js`
- **Storage**: JSON files (config + state), no database needed

## What's Been Implemented
- [x] Full bot logic: long polling, 44 keyword filtering, 8 negative keywords, multi-recipient forwarding
- [x] Telegram commands: /start, /status, /threshold, /llm, /debug, /addchat, /rmchat
- [x] Web dashboard (Swiss brutalist design) with live metrics, logs, restart button
- [x] **Supervised bot lifecycle**: task handle stored, done-callback for crash detection
- [x] **Safe restart**: mutex lock prevents duplicate polling loops, cancel-then-start
- [x] **Truthful health model**: task_alive, polling_active, last_poll_ok, last_error, poll_count, restart_count
- [x] **Removed fake anti-sleep**: Internal self-ping loop removed (cannot prevent container suspension)
- [x] **Fixed Node.js defects**: Status no longer hardcodes "running", uses lastPollOk timestamp, keep-alive removed

## Root Cause Analysis (2026-04-16)
1. Bot task was fire-and-forget (`asyncio.create_task` with no handle) — silent death undetected
2. `self.running` flag was never cleared when task crashed — status lied "running"
3. No heartbeat timestamp — couldn't distinguish active polling from dead process
4. Restart created duplicate polling loops (race with 30s long-poll timeout)
5. Internal self-ping cannot prevent container suspension (same process sleeps together)

## Platform Limitation
Emergent native deployment may suspend pods during inactivity. Code alone cannot prevent this.
**Recommended solutions** (external to code):
- External uptime monitor (e.g. UptimeRobot) pinging the deployed URL
- Telegram Webhook mode (incoming webhooks wake the pod)
- Always-on hosting tier or dedicated worker process

## Files Changed (2026-04-16)
- `backend/telegram_bot.py` — Complete lifecycle rewrite (start/stop/restart with lock, health telemetry, done-callback)
- `backend/server.py` — Proper startup/shutdown, removed internal keep-alive
- `bot/transport-monitor-bot.js` — Fixed status lies, removed keep-alive, added lastPollOk
- `frontend/src/App.js` — Dashboard shows real health data

## Backlog
### P0 - Done
- [x] Supervised bot lifecycle
- [x] Truthful health reporting
- [x] Safe restart without duplicate loops

### P1 - Recommended
- [ ] Webhook mode for Telegram (eliminates long-polling, works with pod suspension)
- [ ] External uptime monitor integration

### P2 - Nice to Have
- [ ] Web UI keyword editor
- [ ] Statistics history charts
- [ ] Message history in MongoDB
