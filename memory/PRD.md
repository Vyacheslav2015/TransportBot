# Transport Monitor Bot - PRD

## Original Problem Statement
Telegram bot for monitoring groups and forwarding messages with keyword filtering. Node.js based, running on Emergent Cloud with Supervisor.

## Architecture
- **Bot**: Node.js (long polling) at `/root/clawd/transport-monitor-bot.js`
- **Backend**: FastAPI (Python) providing status API at `/app/backend/server.py`
- **Frontend**: React dashboard at `/app/frontend/src/App.js`
- **Supervisor**: Bot managed as `transport-bot` service
- **Storage**: JSON files (config + state), no database needed for bot

## Core Requirements
1. Long polling Telegram groups 24/7
2. Keyword-based filtering (44 positive + 8 negative keywords)
3. Message forwarding to multiple recipients with formatting
4. Optional LLM analysis via Claude Sonnet 4.5 (Emergent LLM Key)
5. Telegram commands for management (/status, /threshold, /llm, /debug, /addchat, /rmchat)
6. Auto-recovery (immortal bot - never crashes permanently)
7. Web dashboard for monitoring
8. Rate limiting + deduplication
9. Health check script

## What's Been Implemented (2026-04-15)
- [x] Full bot logic with long polling, keyword scoring, negative keyword blocking
- [x] Multi-recipient forwarding with rate limiting
- [x] Command handling (/start, /status, /threshold, /llm, /debug, /addchat, /rmchat)
- [x] Auto-recovery with 10-second retry on any error
- [x] LLM integration ready (Claude Sonnet 4.5 via Emergent endpoint)
- [x] JSON config and state persistence
- [x] Internal HTTP status server (port 8099)
- [x] FastAPI backend with bot status/config/logs/restart APIs
- [x] React dashboard (Swiss brutalist design) with live metrics
- [x] Supervisor configuration with autorestart
- [x] Health check script
- [x] All tests passed 100% (backend, frontend, infrastructure)

## User Personas
- **Bot Owner**: Manages bot via Telegram commands and web dashboard
- **Recipients**: Receive forwarded transport-related messages

## Configuration
- Bot Token: Set in /root/clawd/transport-bot-config.json
- LLM Key: sk-emergent-15213DaCe768a5629E
- Recipients: 901271393, 5765433747
- Threshold: 1 (configurable 1-10)
- LLM: OFF by default

## Backlog
### P0 (Critical) - Done
- [x] Bot polling and message processing
- [x] Keyword filtering
- [x] Message forwarding
- [x] Auto-recovery

### P1 (Important) - Done
- [x] Web dashboard
- [x] Command handling
- [x] Health check

### P2 (Nice to Have)
- [ ] Webhook support (alternative to polling)
- [ ] Statistics history/charts on dashboard
- [ ] Web UI for editing keywords/config
- [ ] Multiple language support
- [ ] Message history storage in MongoDB

## Next Tasks
- Test bot in real Telegram groups
- Add keyword management via web dashboard
- Add message history/statistics charts
