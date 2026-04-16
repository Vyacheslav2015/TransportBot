"""
FastAPI backend for Transport Monitor Bot dashboard.
The Telegram bot runs as a supervised asyncio task in this same process.

NOTE on anti-sleep: Internal self-pinging cannot prevent container/pod suspension.
If the hosting platform suspends inactive pods, the only real solutions are:
  1. Webhook mode (Telegram pushes to us — any incoming request wakes the pod)
  2. External uptime monitor (e.g. UptimeRobot pinging our /api/ endpoint)
  3. Always-on hosting tier / dedicated worker process
The previous internal keep-alive loop has been removed because it is ineffective.
"""
from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from telegram_bot import bot

app = FastAPI()
api_router = APIRouter(prefix="/api")

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot')


@app.on_event("startup")
async def startup_event():
    logger.info("=== Backend startup ===")
    logger.info(f"TELEGRAM_BOT_TOKEN set: {bool(os.environ.get('TELEGRAM_BOT_TOKEN'))}")
    logger.info(f"APP_URL: {os.environ.get('APP_URL', 'not set')}")
    await bot.start()


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Backend shutting down, stopping bot...")
    await bot.stop()


# ─── API ROUTES ──────────────────────────────────────────────

@api_router.get("/")
async def root():
    return {"message": "Transport Monitor Bot API"}


@api_router.get("/bot/status")
async def get_bot_status():
    return bot.get_status()


@api_router.get("/bot/config")
async def get_bot_config():
    safe = {k: v for k, v in bot.config.items() if k not in ('botToken', 'llmApiKey')}
    safe['botTokenSet'] = bool(bot.config.get('botToken') or os.environ.get('TELEGRAM_BOT_TOKEN'))
    safe['llmApiKeySet'] = bool(bot.config.get('llmApiKey') or os.environ.get('EMERGENT_LLM_KEY'))
    return safe


@api_router.get("/bot/logs")
async def get_bot_logs(lines: int = 50):
    logs = {"stdout": [], "stderr": []}
    import subprocess
    log_paths = [
        ("stdout", "/var/log/supervisor/backend.out.log"),
        ("stderr", "/var/log/supervisor/backend.err.log"),
    ]
    for logtype, filepath in log_paths:
        if not os.path.exists(filepath):
            continue
        try:
            result = subprocess.run(['tail', f'-{lines}', filepath], capture_output=True, text=True, timeout=5)
            if result.stdout.strip():
                logs[logtype].extend(result.stdout.strip().split('\n'))
        except Exception:
            pass
    return logs


@api_router.post("/bot/restart")
async def restart_bot():
    await bot.restart()
    return {
        "success": True,
        "restart_count": bot._restart_count,
        "method": "asyncio (safe restart)",
    }


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
