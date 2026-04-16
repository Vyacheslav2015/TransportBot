"""
Telegram Transport Monitor Bot — Webhook mode.

Architecture: Telegram POSTs updates to /api/bot/webhook/<secret>.
No background polling task. The bot is alive whenever FastAPI is alive.
Pod suspension is fine — Telegram retries delivery when pod wakes up.

Lifecycle:
  init()           → load config/state, register webhook with Telegram
  handle_update()  → called per incoming webhook POST
  shutdown()       → delete webhook from Telegram
"""
import httpx
import asyncio
import json
import os
import logging
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot')
CONFIG_PATH = os.path.join(BOT_DIR, 'transport-bot-config.json')
STATE_PATH = os.path.join(BOT_DIR, 'transport-bot-state.json')
TELEGRAM_API = "https://api.telegram.org"


def _generate_webhook_secret(token: str) -> str:
    """Derive a stable secret from the bot token so we don't need extra env vars."""
    return hashlib.sha256(f"webhook-{token}".encode()).hexdigest()[:32]


class TransportMonitorBot:
    def __init__(self):
        self.config = {}
        self.state = {
            "lastUpdateId": 0,
            "seenMessages": [],
            "lastForwardTime": 0,
            "stats": {"totalProcessed": 0, "totalForwarded": 0, "totalBlocked": 0, "startedAt": None}
        }
        self.webhook_secret = ""
        self._initialized = False
        self._webhook_url = ""  # what we registered with Telegram
        self._state_lock = asyncio.Lock()  # protects state read-modify-write

        # Health telemetry
        self._last_webhook_received: str | None = None
        self._last_update_processed: str | None = None
        self._last_error: str | None = None
        self._webhook_count = 0
        self._started_at: str | None = None

    # ─── CONFIG & STATE ──────────────────────────────────────

    def load_config(self):
        try:
            with open(CONFIG_PATH, 'r') as f:
                self.config = json.load(f)
        except Exception as e:
            logger.warning(f"Config JSON load failed: {e}, using defaults")
            self.config = {}

        if os.environ.get('TELEGRAM_BOT_TOKEN'):
            self.config['botToken'] = os.environ['TELEGRAM_BOT_TOKEN']
        if os.environ.get('EMERGENT_LLM_KEY'):
            self.config['llmApiKey'] = os.environ['EMERGENT_LLM_KEY']

        self.config.setdefault('keywords', [
            "такси", "трансфер", "водитель", "машин", "поездк",
            "довезти", "подвезти", "подвезу", "везу",
            "taxi", "transfer", "driver", "ride", "car",
            "пассажир", "пасажирск", "passenger", "людей", "человек",
            "перевозк", "перевозка", "перевозки", "перевозкой", "перевозок",
            "аэропорт", "вокзал", "airport", "station",
            "маршрут", "рейс", "межгород", "intercity",
            "встреч", "заказ", "авто", "седан", "минивэн",
            "микроавтобус", "minivan", "автобус", "bus",
            "места", "seat", "поездка"
        ])
        self.config.setdefault('negKeywords', [
            "casino", "crypto", "биткоин", "bitcoin",
            "adult", "xxx", "giveaway", "розыгрыш"
        ])
        self.config.setdefault('destUserIds', ["901271393", "5765433747"])
        self.config.setdefault('threshold', 1)
        self.config.setdefault('rateLimit', 1000)
        self.config.setdefault('useLLM', False)
        self.config.setdefault('llmModel', 'claude-sonnet-4-5-20250929')
        self.config.setdefault('debug', True)
        self.config.setdefault('allowedChats', [])
        logger.info(f"Config loaded: token={'SET' if self.config.get('botToken') else 'MISSING'}, kw={len(self.config['keywords'])}")

    def save_config(self):
        try:
            safe = {k: v for k, v in self.config.items() if k not in ('botToken', 'llmApiKey')}
            safe['botToken'] = ''
            safe['llmApiKey'] = ''
            with open(CONFIG_PATH, 'w') as f:
                json.dump(safe, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to save config: {e}")

    def load_state(self):
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, 'r') as f:
                    self.state = json.load(f)
            if 'stats' not in self.state:
                self.state['stats'] = {"totalProcessed": 0, "totalForwarded": 0, "totalBlocked": 0, "startedAt": None}
            logger.info(f"State loaded: lastUpdateId={self.state['lastUpdateId']}, seen={len(self.state.get('seenMessages', []))}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def save_state(self):
        """Non-async save for use inside already-locked contexts."""
        try:
            if len(self.state.get('seenMessages', [])) > 1000:
                self.state['seenMessages'] = self.state['seenMessages'][-1000:]
            with open(STATE_PATH, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception:
            pass

    # ─── TELEGRAM API ────────────────────────────────────────

    async def telegram_api(self, method, params=None):
        token = self.config.get('botToken', '')
        url = f"{TELEGRAM_API}/bot{token}/{method}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=params or {})
            data = resp.json()
            if data.get('ok'):
                return data.get('result')
            raise Exception(f"Telegram API: {data.get('description', 'Unknown error')}")

    # ─── WEBHOOK REGISTRATION ────────────────────────────────

    async def register_webhook(self, base_url: str):
        """Register webhook with Telegram. Called on startup."""
        webhook_url = f"{base_url.rstrip('/')}/api/bot/webhook/{self.webhook_secret}"
        logger.info(f"Registering webhook: {webhook_url}")
        try:
            result = await self.telegram_api('setWebhook', {
                'url': webhook_url,
                'allowed_updates': ['message', 'channel_post'],
                'drop_pending_updates': False,
                'secret_token': self.webhook_secret,
            })
            self._webhook_url = webhook_url
            logger.info(f"Webhook registered: {result}")
            return True
        except Exception as e:
            self._last_error = f"{datetime.now(timezone.utc).isoformat()} Webhook registration failed: {e}"
            logger.error(f"Webhook registration failed: {e}")
            return False

    async def delete_webhook(self):
        """Delete webhook from Telegram. Called on shutdown."""
        try:
            await self.telegram_api('deleteWebhook', {'drop_pending_updates': False})
            logger.info("Webhook deleted")
        except Exception as e:
            logger.warning(f"Webhook delete failed: {e}")

    # ─── INITIALIZATION ──────────────────────────────────────

    async def init(self, base_url: str):
        """Initialize bot: load config, register webhook."""
        self.load_config()
        self.load_state()

        if not self.config.get('botToken'):
            logger.error("NO BOT TOKEN — cannot start bot")
            return False

        self.webhook_secret = _generate_webhook_secret(self.config['botToken'])
        self._started_at = datetime.now(timezone.utc).isoformat()
        self.state['stats']['startedAt'] = self._started_at

        logger.info("=== Transport Monitor Bot (Webhook mode) ===")
        logger.info(f"Token: {self.config['botToken'][:10]}...")
        logger.info(f"Recipients: {', '.join(self.config.get('destUserIds', []))}")
        logger.info(f"Keywords: {len(self.config.get('keywords', []))} pos, {len(self.config.get('negKeywords', []))} neg")

        ok = await self.register_webhook(base_url)

        # Startup notification
        try:
            mode = "webhook" if ok else "webhook (FAILED to register)"
            for uid in self.config.get('destUserIds', []):
                await self.telegram_api('sendMessage', {
                    'chat_id': uid,
                    'text': f'\u2705 Transport Monitor Bot started!\nMode: {mode}\n\n/status - check config\n/start - all commands'
                })
        except Exception as e:
            logger.warning(f"Startup notification failed: {e}")

        self._initialized = ok
        return ok

    async def shutdown(self):
        """Graceful shutdown. Does NOT delete webhook — keeps Telegram delivering
        so the pod can be woken up on next message. Use reset_webhook() for
        explicit admin removal."""
        self._initialized = False
        logger.info("Bot shutdown (webhook kept registered for pod wake-up)")

    # ─── MESSAGE LOGIC ───────────────────────────────────────

    def score_message(self, text):
        lower = text.lower()
        for neg in self.config.get('negKeywords', []):
            if neg.lower() in lower:
                return {"score": -1, "blocked": True, "matchedNeg": neg}
        score = 0
        matched = []
        for kw in self.config.get('keywords', []):
            if kw.lower() in lower:
                score += 1
                matched.append(kw)
        return {"score": score, "blocked": False, "matched": matched}

    async def forward_message(self, msg, score, llm_result, matched):
        chat_title = msg.get('chat', {}).get('title') or msg.get('chat', {}).get('username') or f"ID:{msg.get('chat', {}).get('id')}"
        from_user = msg.get('from', {})
        user_name = f"@{from_user['username']}" if from_user.get('username') else f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or 'Unknown'
        text = msg.get('text') or msg.get('caption') or ''
        llm_status = 'OFF'
        if self.config.get('useLLM'):
            llm_status = 'YES' if llm_result else 'NO'
        forward_text = (
            f"\U0001F697 Новый запрос\n\n"
            f"\U0001F4CD Группа: {chat_title}\n"
            f"\U0001F464 От: {user_name}\n"
            f"\U0001F4CA Score: {score} | LLM: {llm_status}\n"
        )
        if matched:
            forward_text += f"\U0001F50D Слова: {', '.join(matched)}\n"
        forward_text += f"\n{text[:3000]}"
        for rid in self.config.get('destUserIds', []):
            try:
                now = time.time() * 1000
                elapsed = now - self.state.get('lastForwardTime', 0)
                if elapsed < self.config.get('rateLimit', 1000):
                    await asyncio.sleep((self.config['rateLimit'] - elapsed) / 1000)
                await self.telegram_api('sendMessage', {'chat_id': rid, 'text': forward_text, 'disable_web_page_preview': True})
                self.state['lastForwardTime'] = time.time() * 1000
                if self.config.get('debug'):
                    logger.info(f"Forwarded to {rid}")
            except Exception as e:
                logger.warning(f"Forward to {rid} failed: {e}")
        self.state['stats']['totalForwarded'] += 1

    def is_owner(self, user_id):
        return str(user_id) in (self.config.get('destUserIds') or [])

    async def handle_command(self, msg):
        user_id = msg.get('from', {}).get('id')
        if not user_id or not self.is_owner(user_id):
            return False
        text = (msg.get('text') or '').strip()
        if not text.startswith('/'):
            return False
        parts = text.split()
        cmd = parts[0].lower().split('@')[0]
        arg = parts[1] if len(parts) > 1 else ''
        reply = ''
        if cmd == '/start':
            reply = "\U0001F697 Transport Monitor Bot\n\nКоманды:\n/status - Текущая конфигурация\n/threshold <n> - Порог (1-10)\n/llm on|off - LLM анализ\n/debug on|off - Debug логи\n/addchat <id> - Добавить группу\n/rmchat <id> - Удалить группу"
        elif cmd == '/status':
            s = self.state.get('stats', {})
            wh_ago = ''
            if self._last_webhook_received:
                secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(self._last_webhook_received)).total_seconds())
                wh_ago = f" ({secs}s ago)"
            reply = (
                f"\U0001F4CA Status:\n\n"
                f"\U0001F527 Mode: Webhook"
                f"{' + LLM' if self.config.get('useLLM') else ''}\n"
                f"\U0001F4CB Allowed chats: {', '.join(self.config.get('allowedChats', [])) or 'ALL'}\n"
                f"\U0001F3AF Threshold: {self.config.get('threshold', 1)}\n"
                f"\U0001F511 Keywords: {len(self.config.get('keywords', []))}\n"
                f"\U0001F6AB Negative: {len(self.config.get('negKeywords', []))}\n"
                f"\U0001F465 Recipients: {len(self.config.get('destUserIds', []))}\n"
                f"\U0001F4BE Seen: {len(self.state.get('seenMessages', []))}\n"
                f"\U0001F50D Debug: {'ON' if self.config.get('debug') else 'OFF'}\n"
                f"\U0001F9E0 LLM: {'ON' if self.config.get('useLLM') else 'OFF'}\n"
                f"\U0001F4E1 Last webhook: {wh_ago or 'none'}\n"
                f"\U0001F504 Webhooks received: {self._webhook_count}\n\n"
                f"\U0001F4C8 Stats:\n  Processed: {s.get('totalProcessed', 0)}\n  Forwarded: {s.get('totalForwarded', 0)}\n  Blocked: {s.get('totalBlocked', 0)}"
            )
        elif cmd == '/threshold':
            try:
                n = int(arg)
                if 1 <= n <= 10:
                    self.config['threshold'] = n
                    self.save_config()
                    reply = f"\u2705 Threshold set to {n}"
                else:
                    reply = "\u274C Usage: /threshold <1-10>"
            except ValueError:
                reply = "\u274C Usage: /threshold <1-10>"
        elif cmd == '/llm':
            if arg == 'on':
                self.config['useLLM'] = True
                self.save_config()
                reply = "\u2705 LLM analysis enabled"
            elif arg == 'off':
                self.config['useLLM'] = False
                self.save_config()
                reply = "\u2705 LLM analysis disabled"
            else:
                reply = "\u274C Usage: /llm on|off"
        elif cmd == '/debug':
            if arg == 'on':
                self.config['debug'] = True
                self.save_config()
                reply = "\u2705 Debug ON"
            elif arg == 'off':
                self.config['debug'] = False
                self.save_config()
                reply = "\u2705 Debug OFF"
            else:
                reply = "\u274C Usage: /debug on|off"
        elif cmd == '/addchat':
            if arg:
                cid = arg if arg.startswith('-') else f"-{arg}"
                self.config.setdefault('allowedChats', [])
                if cid not in self.config['allowedChats']:
                    self.config['allowedChats'].append(cid)
                    self.save_config()
                    reply = f"\u2705 Chat {cid} added"
                else:
                    reply = "\u2139\ufe0f Already in whitelist"
            else:
                reply = "\u274C Usage: /addchat <chat_id>"
        elif cmd == '/rmchat':
            if arg:
                cid = arg if arg.startswith('-') else f"-{arg}"
                self.config.setdefault('allowedChats', [])
                self.config['allowedChats'] = [c for c in self.config['allowedChats'] if c != cid]
                self.save_config()
                reply = f"\u2705 Chat {cid} removed"
            else:
                reply = "\u274C Usage: /rmchat <chat_id>"
        else:
            return False
        if reply:
            try:
                await self.telegram_api('sendMessage', {'chat_id': msg['chat']['id'], 'text': reply})
            except Exception as e:
                logger.warning(f"Command reply failed: {e}")
        return True

    # ─── WEBHOOK HANDLER ─────────────────────────────────────

    async def handle_update(self, update: dict):
        """Process a single update from Telegram webhook.
        Raises on internal failure so the caller can return non-200 to Telegram."""
        now = datetime.now(timezone.utc).isoformat()
        self._last_webhook_received = now
        self._webhook_count += 1

        msg = update.get('message') or update.get('channel_post')
        if not msg:
            return  # not a message type we handle — 200 OK is correct
        text = msg.get('text') or msg.get('caption') or ''
        if not text:
            return  # no text content — 200 OK is correct

        async with self._state_lock:
            msg_key = f"{msg['chat']['id']}:{msg['message_id']}"
            if msg_key in self.state.get('seenMessages', []):
                return  # duplicate — 200 OK
            self.state.setdefault('seenMessages', []).append(msg_key)

            uid = update.get('update_id', 0)
            if uid > self.state.get('lastUpdateId', 0):
                self.state['lastUpdateId'] = uid

            self._last_update_processed = now

            # Commands in private chat
            if msg.get('chat', {}).get('type') == 'private':
                if await self.handle_command(msg):
                    self.save_state()
                    return

            # Chat whitelist
            allowed = self.config.get('allowedChats', [])
            if allowed and str(msg['chat']['id']) not in allowed:
                self.save_state()
                return

            # Skip private non-command messages
            if msg.get('chat', {}).get('type') == 'private':
                self.save_state()
                return

            self.state['stats']['totalProcessed'] += 1
            result = self.score_message(text)

            if result['blocked']:
                self.state['stats']['totalBlocked'] += 1
                if self.config.get('debug'):
                    logger.info(f"BLOCKED: '{result['matchedNeg']}' in: {text[:80]}")
                self.save_state()
                return

            if self.config.get('debug'):
                logger.info(f"Score: {result['score']}/{self.config.get('threshold', 1)} for: {text[:80]}... [{', '.join(result.get('matched', []))}]")

            if result['score'] < self.config.get('threshold', 1):
                self.save_state()
                return

            # forward_message can raise — let it propagate for Telegram retry
            await self.forward_message(msg, result['score'], None, result.get('matched'))
            self.save_state()

    # ─── STATUS ──────────────────────────────────────────────

    async def fetch_telegram_webhook_info(self):
        """Fetch real webhook state from Telegram. Returns dict or None on error."""
        try:
            info = await self.telegram_api('getWebhookInfo')
            return {
                "url_set": bool(info.get('url')),
                "url_matches": info.get('url', '') == self._webhook_url,
                "pending_update_count": info.get('pending_update_count', 0),
                "last_error_date": info.get('last_error_date'),
                "last_error_message": info.get('last_error_message'),
            }
        except Exception:
            return None

    def get_status(self):
        uptime = 0
        if self._started_at:
            try:
                uptime = int((datetime.now(timezone.utc) - datetime.fromisoformat(self._started_at)).total_seconds())
            except Exception:
                pass

        return {
            "status": "running" if self._initialized else "stopped",
            "mode": "webhook",
            "uptime": uptime,
            "health": {
                "initialized": self._initialized,
                "webhook_url_registered": bool(self._webhook_url),
                "last_webhook_received": self._last_webhook_received,
                "last_update_processed": self._last_update_processed,
                "last_error": self._last_error,
                "webhook_count": self._webhook_count,
            },
            "config": {
                "useLLM": self.config.get('useLLM', False),
                "debug": self.config.get('debug', True),
                "threshold": self.config.get('threshold', 1),
                "keywordsCount": len(self.config.get('keywords', [])),
                "negKeywordsCount": len(self.config.get('negKeywords', [])),
                "recipientsCount": len(self.config.get('destUserIds', [])),
                "allowedChats": self.config.get('allowedChats') or 'ALL',
                "botTokenSet": bool(self.config.get('botToken')),
                "llmApiKeySet": bool(self.config.get('llmApiKey'))
            },
            "stats": self.state.get('stats', {}),
            "seenMessages": len(self.state.get('seenMessages', [])),
            "lastUpdateId": self.state.get('lastUpdateId', 0),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


# Global singleton
bot = TransportMonitorBot()
