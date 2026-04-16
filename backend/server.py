from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import json
import subprocess
import asyncio
from pathlib import Path
from datetime import datetime, timezone
import httpx

# Configure logging FIRST
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

app = FastAPI()
api_router = APIRouter(prefix="/api")

BOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot')
BOT_SCRIPT = os.path.join(BOT_DIR, 'transport-monitor-bot.js')
CONFIG_PATH = os.path.join(BOT_DIR, 'transport-bot-config.json')
STATE_PATH = os.path.join(BOT_DIR, 'transport-bot-state.json')
BOT_STATUS_URL = 'http://127.0.0.1:8099/health'
BOT_LOG_FILE = os.path.join(BOT_DIR, 'bot.log')
BOT_ERR_FILE = os.path.join(BOT_DIR, 'bot.err.log')

bot_process = None

def read_json_file(filepath):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception:
        return None

def start_bot_process():
    global bot_process
    if bot_process and bot_process.poll() is None:
        logger.info("Bot already running (PID: %s)", bot_process.pid)
        return
    if not os.path.exists(BOT_SCRIPT):
        logger.error("Bot script not found: %s", BOT_SCRIPT)
        return
    try:
        stdout_f = open(BOT_LOG_FILE, 'a')
        stderr_f = open(BOT_ERR_FILE, 'a')
        bot_env = {**os.environ, 'NODE_ENV': 'production'}
        # Pass credentials from backend env to bot process
        if os.environ.get('TELEGRAM_BOT_TOKEN'):
            bot_env['TELEGRAM_BOT_TOKEN'] = os.environ['TELEGRAM_BOT_TOKEN']
        if os.environ.get('EMERGENT_LLM_KEY'):
            bot_env['EMERGENT_LLM_KEY'] = os.environ['EMERGENT_LLM_KEY']
        if os.environ.get('APP_URL'):
            bot_env['APP_URL'] = os.environ['APP_URL']
        bot_process = subprocess.Popen(
            ['node', BOT_SCRIPT],
            cwd=BOT_DIR,
            stdout=stdout_f,
            stderr=stderr_f,
            env=bot_env
        )
        logger.info("Bot started as subprocess (PID: %s)", bot_process.pid)
    except Exception as e:
        logger.error("Failed to start bot: %s", e)

def stop_bot_process():
    global bot_process
    if bot_process and bot_process.poll() is None:
        logger.info("Stopping bot (PID: %s)", bot_process.pid)
        bot_process.terminate()
        try:
            bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot_process.kill()
    bot_process = None

def is_supervisor_managing_bot():
    """Check if supervisor is managing the bot (works in preview, not in prod)"""
    try:
        result = subprocess.run(
            ['supervisorctl', 'status', 'transport-bot'],
            capture_output=True, text=True, timeout=3
        )
        return 'RUNNING' in result.stdout
    except Exception:
        pass
    # Try with sudo (preview env)
    try:
        result = subprocess.run(
            ['sudo', 'supervisorctl', 'status', 'transport-bot'],
            capture_output=True, text=True, timeout=3
        )
        return 'RUNNING' in result.stdout
    except Exception:
        return False

def ensure_bot_running():
    """Start bot if not already running"""
    global bot_process
    if is_supervisor_managing_bot():
        logger.info("Bot managed by supervisor")
        return
    # Fallback: manage as subprocess
    if bot_process is None or bot_process.poll() is not None:
        logger.info("Starting bot as subprocess...")
        start_bot_process()

@app.on_event("startup")
async def startup_event():
    logger.info("Backend starting, ensuring bot is running...")
    ensure_bot_running()
    asyncio.create_task(keep_alive_loop())
    asyncio.create_task(bot_watchdog())

async def keep_alive_loop():
    """Ping self every 60 seconds to keep pod alive"""
    app_url = os.environ.get('APP_URL', '')
    if not app_url:
        logger.info("No APP_URL, keep-alive disabled")
        return
    ping_url = app_url.rstrip('/') + '/api/'
    logger.info(f"Keep-alive target: {ping_url} (every 60s)")
    while True:
        await asyncio.sleep(60)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(ping_url, headers={"User-Agent": "TransportBot-KeepAlive/2.0"})
                logger.debug(f"Keep-alive: {resp.status_code}")
        except Exception:
            pass

async def bot_watchdog():
    """Check bot subprocess health every 30 seconds, restart if crashed"""
    global bot_process
    while True:
        await asyncio.sleep(30)
        if is_supervisor_managing_bot():
            continue
        if bot_process is None or bot_process.poll() is not None:
            exit_code = bot_process.poll() if bot_process else 'N/A'
            logger.warning(f"Bot subprocess died (exit: {exit_code}), restarting...")
            start_bot_process()

@app.on_event("shutdown")
async def shutdown_event():
    stop_bot_process()

@api_router.get("/")
async def root():
    return {"message": "Transport Monitor Bot API"}

@api_router.get("/bot/status")
async def get_bot_status():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(BOT_STATUS_URL)
            return resp.json()
    except Exception:
        return {
            "status": "offline",
            "error": "Bot status server not responding",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

@api_router.get("/bot/config")
async def get_bot_config():
    config = read_json_file(CONFIG_PATH)
    if config:
        safe_config = {k: v for k, v in config.items() if k not in ('botToken', 'llmApiKey')}
        safe_config['botTokenSet'] = bool(config.get('botToken') or os.environ.get('TELEGRAM_BOT_TOKEN'))
        safe_config['llmApiKeySet'] = bool(config.get('llmApiKey') or os.environ.get('EMERGENT_LLM_KEY'))
        return safe_config
    return {"error": "Config not found"}

@api_router.get("/bot/logs")
async def get_bot_logs(lines: int = 50):
    logs = {"stdout": [], "stderr": []}
    log_paths = [
        ("stdout", "/var/log/supervisor/transport-bot.out.log"),
        ("stderr", "/var/log/supervisor/transport-bot.err.log"),
        ("stdout", BOT_LOG_FILE),
        ("stderr", BOT_ERR_FILE),
    ]
    for logtype, filepath in log_paths:
        if not os.path.exists(filepath):
            continue
        try:
            result = subprocess.run(['tail', f'-{lines}', filepath], capture_output=True, text=True, timeout=5)
            if result.stdout.strip():
                existing = logs.get(logtype, [])
                existing.extend(result.stdout.strip().split('\n'))
                logs[logtype] = existing
        except Exception:
            pass
    return logs

@api_router.post("/bot/restart")
async def restart_bot():
    global bot_process
    # Try supervisor (without sudo first, then with sudo)
    for cmd_prefix in [[], ['sudo']]:
        try:
            result = subprocess.run(
                cmd_prefix + ['supervisorctl', 'restart', 'transport-bot'],
                capture_output=True, text=True, timeout=10
            )
            if 'started' in result.stdout.lower():
                return {"success": True, "output": result.stdout.strip(), "method": "supervisor"}
        except Exception:
            continue
    # Fallback: restart subprocess
    stop_bot_process()
    start_bot_process()
    return {"success": True, "output": f"Bot restarted (PID: {bot_process.pid if bot_process else 'N/A'})", "method": "subprocess"}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
