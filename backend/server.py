from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from telegram_bot import bot

app = FastAPI()
api_router = APIRouter(prefix="/api")

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot')
BOT_LOG_FILE = os.path.join(BOT_DIR, 'bot.log')

@app.on_event("startup")
async def startup_event():
    logger.info("=== Backend startup ===")
    logger.info(f"TELEGRAM_BOT_TOKEN set: {bool(os.environ.get('TELEGRAM_BOT_TOKEN'))}")
    logger.info(f"APP_URL: {os.environ.get('APP_URL', 'not set')}")
    # Start bot as asyncio task in the same process
    asyncio.create_task(bot.run())
    asyncio.create_task(keep_alive_loop())
    logger.info("Bot task started")

async def keep_alive_loop():
    app_url = os.environ.get('APP_URL', '')
    if not app_url:
        return
    ping_url = app_url.rstrip('/') + '/api/'
    logger.info(f"Keep-alive: {ping_url} (60s)")
    while True:
        await asyncio.sleep(60)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(ping_url)
        except Exception:
            pass

@app.on_event("shutdown")
async def shutdown_event():
    bot.stop()

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
        ("stdout", "/var/log/supervisor/transport-bot.out.log"),
        ("stderr", "/var/log/supervisor/transport-bot.err.log"),
        ("stdout", BOT_LOG_FILE),
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
    bot.stop()
    await asyncio.sleep(1)
    asyncio.create_task(bot.run())
    return {"success": True, "output": "Bot restarted (in-process)", "method": "asyncio"}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
