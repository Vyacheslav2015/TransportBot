"""
FastAPI backend for Transport Monitor Bot dashboard.
The Telegram bot runs in WEBHOOK mode — no background polling task.
Telegram pushes updates to POST /api/bot/webhook/<secret>.

This is compatible with pod suspension: incoming webhooks wake the pod.
"""
from fastapi import FastAPI, APIRouter, Request, Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from telegram_bot import bot

app = FastAPI()
api_router = APIRouter(prefix="/api")


def _get_app_url() -> str:
    """Get the external app URL for webhook registration."""
    return os.environ.get('APP_URL', '').rstrip('/')


@app.on_event("startup")
async def startup_event():
    logger.info("=== Backend startup (webhook mode) ===")
    logger.info(f"TELEGRAM_BOT_TOKEN set: {bool(os.environ.get('TELEGRAM_BOT_TOKEN'))}")
    app_url = _get_app_url()
    logger.info(f"APP_URL: {app_url or 'NOT SET'}")
    if app_url:
        ok = await bot.init(app_url)
        logger.info(f"Bot init: {'OK' if ok else 'FAILED'}")
    else:
        logger.error("APP_URL not set — cannot register webhook. Bot will not receive updates.")
        bot.load_config()
        bot.load_state()


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Backend shutting down...")
    await bot.shutdown()


# ─── WEBHOOK ENDPOINT ────────────────────────────────────────

@api_router.post("/bot/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Receives updates from Telegram. Validates secret token."""
    if secret != bot.webhook_secret:
        return Response(status_code=403)

    # Telegram also sends secret in header — double-check
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret and header_secret != bot.webhook_secret:
        return Response(status_code=403)

    try:
        update = await request.json()
        await bot.handle_update(update)
    except Exception as e:
        logger.error(f"Webhook handler error: {e}")
        bot._last_error = f"{e}"

    # Always return 200 to Telegram (otherwise it retries aggressively)
    return Response(status_code=200)


# ─── API ROUTES ──────────────────────────────────────────────

@api_router.get("/")
async def root():
    return {"message": "Transport Monitor Bot API (webhook mode)"}


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
    import subprocess as sp
    logs = {"stdout": [], "stderr": []}
    for logtype, filepath in [("stdout", "/var/log/supervisor/backend.out.log"), ("stderr", "/var/log/supervisor/backend.err.log")]:
        if not os.path.exists(filepath):
            continue
        try:
            result = sp.run(['tail', f'-{lines}', filepath], capture_output=True, text=True, timeout=5)
            if result.stdout.strip():
                logs[logtype].extend(result.stdout.strip().split('\n'))
        except Exception:
            pass
    return logs


@api_router.post("/bot/restart")
async def restart_bot():
    """Re-register webhook. No polling task to restart."""
    app_url = _get_app_url()
    if not app_url:
        return {"success": False, "error": "APP_URL not set"}
    await bot.delete_webhook()
    bot.load_config()
    ok = await bot.register_webhook(app_url)
    return {"success": ok, "method": "webhook re-registration"}


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
