"""
Telegram Transport Monitor Bot — Python asyncio implementation.
Runs as a supervised background task inside the FastAPI process.

Lifecycle: start() → creates asyncio task → poll loop → stop() cancels it.
Health: tracks last_poll_ok, last_error, task alive/dead via get_status().
"""
import httpx
import asyncio
import json
import os
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot')
CONFIG_PATH = os.path.join(BOT_DIR, 'transport-bot-config.json')
STATE_PATH = os.path.join(BOT_DIR, 'transport-bot-state.json')
TELEGRAM_API = "https://api.telegram.org"


class TransportMonitorBot:
    def __init__(self):
        self.config = {}
        self.state = {
            "lastUpdateId": 0,
            "seenMessages": [],
            "lastForwardTime": 0,
            "stats": {"totalProcessed": 0, "totalForwarded": 0, "totalBlocked": 0, "startedAt": None}
        }
        # Lifecycle
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # prevents concurrent start/stop
        self._polling = False  # True only while poll loop is actively running

        # Health telemetry
        self._last_poll_ok: str | None = None  # ISO timestamp of last successful poll cycle
        self._last_update_received: str | None = None  # ISO timestamp of last Telegram update
        self._last_error: str | None = None
        self._restart_count = 0
        self._poll_count = 0
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
        timeout = 35.0 if method == 'getUpdates' else 15.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=params or {})
            data = resp.json()
            if data.get('ok'):
                return data.get('result')
            raise Exception(f"Telegram API: {data.get('description', 'Unknown error')}")

    # ─── MESSAGE LOGIC (unchanged business logic) ────────────

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
            poll_ago = ''
            if self._last_poll_ok:
                secs = int((datetime.now(timezone.utc) - datetime.fromisoformat(self._last_poll_ok)).total_seconds())
                poll_ago = f" ({secs}s ago)"
            reply = (
                f"\U0001F4CA Status:\n\n"
                f"\U0001F527 Mode: {'Keywords + LLM' if self.config.get('useLLM') else 'Keywords only'}\n"
                f"\U0001F4CB Allowed chats: {', '.join(self.config.get('allowedChats', [])) or 'ALL'}\n"
                f"\U0001F3AF Threshold: {self.config.get('threshold', 1)}\n"
                f"\U0001F511 Keywords: {len(self.config.get('keywords', []))}\n"
                f"\U0001F6AB Negative: {len(self.config.get('negKeywords', []))}\n"
                f"\U0001F465 Recipients: {len(self.config.get('destUserIds', []))}\n"
                f"\U0001F4BE Seen: {len(self.state.get('seenMessages', []))}\n"
                f"\U0001F50D Debug: {'ON' if self.config.get('debug') else 'OFF'}\n"
                f"\U0001F9E0 LLM: {'ON' if self.config.get('useLLM') else 'OFF'}\n"
                f"\U0001F4E1 Polling: {'active' if self._polling else 'DEAD'}{poll_ago}\n"
                f"\U0001F504 Polls: {self._poll_count}\n\n"
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

    async def process_update(self, update):
        msg = update.get('message') or update.get('channel_post')
        if not msg:
            return
        text = msg.get('text') or msg.get('caption') or ''
        if not text:
            return
        msg_key = f"{msg['chat']['id']}:{msg['message_id']}"
        if msg_key in self.state.get('seenMessages', []):
            return
        self.state.setdefault('seenMessages', []).append(msg_key)
        if msg.get('chat', {}).get('type') == 'private':
            if await self.handle_command(msg):
                return
        allowed = self.config.get('allowedChats', [])
        if allowed and str(msg['chat']['id']) not in allowed:
            return
        if msg.get('chat', {}).get('type') == 'private':
            return
        self.state['stats']['totalProcessed'] += 1
        result = self.score_message(text)
        if result['blocked']:
            self.state['stats']['totalBlocked'] += 1
            if self.config.get('debug'):
                logger.info(f"BLOCKED: '{result['matchedNeg']}' in: {text[:80]}")
            return
        if self.config.get('debug'):
            logger.info(f"Score: {result['score']}/{self.config.get('threshold', 1)} for: {text[:80]}... [{', '.join(result.get('matched', []))}]")
        if result['score'] < self.config.get('threshold', 1):
            return
        await self.forward_message(msg, result['score'], None, result.get('matched'))

    # ─── POLLING ─────────────────────────────────────────────

    async def poll(self):
        updates = await self.telegram_api('getUpdates', {
            'offset': self.state.get('lastUpdateId', 0) + 1,
            'timeout': 30,
            'allowed_updates': ['message', 'channel_post']
        })
        self._last_poll_ok = datetime.now(timezone.utc).isoformat()
        self._poll_count += 1
        if updates:
            self._last_update_received = datetime.now(timezone.utc).isoformat()
            if self.config.get('debug'):
                logger.info(f"Received {len(updates)} update(s)")
            for u in updates:
                self.state['lastUpdateId'] = u['update_id']
                try:
                    await self.process_update(u)
                except Exception as e:
                    logger.warning(f"Error processing update: {e}")
            self.save_state()

    async def _poll_loop(self):
        """The inner polling loop. Runs until cancelled or self._polling is cleared."""
        self.load_config()
        self.load_state()

        if not self.config.get('botToken'):
            logger.error("NO BOT TOKEN — cannot start bot")
            return

        self._polling = True
        self._started_at = datetime.now(timezone.utc).isoformat()
        self.state['stats']['startedAt'] = self._started_at

        logger.info("=== Transport Monitor Bot starting (Python) ===")
        logger.info(f"Token: {self.config['botToken'][:10]}...")
        logger.info(f"Recipients: {', '.join(self.config.get('destUserIds', []))}")
        logger.info(f"Keywords: {len(self.config.get('keywords', []))} pos, {len(self.config.get('negKeywords', []))} neg")

        try:
            for uid in self.config.get('destUserIds', []):
                await self.telegram_api('sendMessage', {
                    'chat_id': uid,
                    'text': '\u2705 Transport Monitor Bot started!\n\n/status - check config\n/start - all commands'
                })
        except Exception as e:
            logger.warning(f"Startup notification failed: {e}")

        try:
            while self._polling:
                try:
                    await self.poll()
                except asyncio.CancelledError:
                    raise  # let cancellation propagate
                except Exception as e:
                    self._last_error = f"{datetime.now(timezone.utc).isoformat()} {e}"
                    logger.error(f"Poll error, retry in 10s: {e}")
                    await asyncio.sleep(10)
        finally:
            self._polling = False
            logger.info("Bot polling loop stopped")

    # ─── PUBLIC LIFECYCLE API ────────────────────────────────

    async def start(self):
        """Start the bot polling task. Safe to call multiple times."""
        async with self._lock:
            if self._task and not self._task.done():
                logger.info("Bot already running, ignoring duplicate start")
                return
            logger.info("Starting bot task...")
            self._task = asyncio.create_task(self._poll_loop(), name="telegram-bot")
            self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task):
        """Callback when the bot task finishes (crash or clean stop)."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.info("Bot task was cancelled (clean shutdown)")
            return
        if exc:
            self._last_error = f"{datetime.now(timezone.utc).isoformat()} TASK CRASHED: {exc}"
            logger.error(f"Bot task CRASHED: {exc}")
        else:
            logger.info("Bot task exited normally")

    async def stop(self):
        """Stop the bot polling task. Waits for clean exit."""
        async with self._lock:
            self._polling = False
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await asyncio.wait_for(self._task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            self._task = None
            logger.info("Bot stopped")

    async def restart(self):
        """Safe restart: fully stop, then start. No duplicate loops."""
        self._restart_count += 1
        logger.info(f"Bot restart #{self._restart_count}")
        await self.stop()
        await self.start()

    # ─── STATUS ──────────────────────────────────────────────

    def get_status(self):
        task_alive = self._task is not None and not self._task.done()
        # Determine real status
        if task_alive and self._polling:
            status = "running"
        elif task_alive and not self._polling:
            status = "starting"
        else:
            status = "stopped"

        uptime = 0
        if self._started_at:
            try:
                uptime = int((datetime.now(timezone.utc) - datetime.fromisoformat(self._started_at)).total_seconds())
            except Exception:
                pass

        return {
            "status": status,
            "uptime": uptime,
            "health": {
                "task_alive": task_alive,
                "polling_active": self._polling,
                "last_poll_ok": self._last_poll_ok,
                "last_update_received": self._last_update_received,
                "last_error": self._last_error,
                "poll_count": self._poll_count,
                "restart_count": self._restart_count,
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
