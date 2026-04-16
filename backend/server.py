from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import json
import subprocess
import asyncio
import shutil
from pathlib import Path
from datetime import datetime, timezone
import httpx

# Configure logging FIRST - all logs go to stdout (visible in deployment)
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
bot_start_attempts = 0
bot_last_error = None

def read_json_file(filepath):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception:
        return None

def find_node_binary():
    """Find node binary path"""
    node_path = shutil.which('node')
    if node_path:
        return node_path
    for p in ['/usr/bin/node', '/usr/local/bin/node', '/root/.nvm/versions/node/v20.20.2/bin/node']:
        if os.path.exists(p):
            return p
    return None

def start_bot_process():
    global bot_process, bot_start_attempts, bot_last_error
    bot_start_attempts += 1

    if bot_process and bot_process.poll() is None:
        logger.info("Bot already running (PID: %s)", bot_process.pid)
        return True

    # Check prerequisites
    node_bin = find_node_binary()
    if not node_bin:
        bot_last_error = "Node.js binary not found in PATH"
        logger.error(bot_last_error)
        return False

    if not os.path.exists(BOT_SCRIPT):
        bot_last_error = f"Bot script not found: {BOT_SCRIPT}"
        logger.error(bot_last_error)
        return False

    # Check token availability
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        config = read_json_file(CONFIG_PATH)
        token = config.get('botToken', '') if config else ''
    if not token:
        bot_last_error = "TELEGRAM_BOT_TOKEN not set in env or config"
        logger.error(bot_last_error)
        return False

    try:
        # Ensure log directory is writable
        os.makedirs(BOT_DIR, exist_ok=True)
        stdout_f = open(BOT_LOG_FILE, 'a')
        stderr_f = open(BOT_ERR_FILE, 'a')

        bot_env = {**os.environ, 'NODE_ENV': 'production'}

        logger.info(f"Starting bot: {node_bin} {BOT_SCRIPT}")
        logger.info(f"TELEGRAM_BOT_TOKEN set: {bool(os.environ.get('TELEGRAM_BOT_TOKEN'))}")
        logger.info(f"EMERGENT_LLM_KEY set: {bool(os.environ.get('EMERGENT_LLM_KEY'))}")
        logger.info(f"APP_URL: {os.environ.get('APP_URL', 'not set')}")

        bot_process = subprocess.Popen(
            [node_bin, BOT_SCRIPT],
            cwd=BOT_DIR,
            stdout=stdout_f,
            stderr=stderr_f,
            env=bot_env
        )
        logger.info("Bot subprocess started (PID: %s)", bot_process.pid)

        # Wait briefly and check if it crashed immediately
        import time
        time.sleep(2)
        if bot_process.poll() is not None:
            exit_code = bot_process.poll()
            # Read error output
            try:
                with open(BOT_ERR_FILE, 'r') as f:
                    err_content = f.read()[-500:]
                bot_last_error = f"Bot crashed on start (exit: {exit_code}): {err_content}"
            except Exception:
                bot_last_error = f"Bot crashed on start (exit: {exit_code})"
            logger.error(bot_last_error)
            return False

        bot_last_error = None
        logger.info("Bot subprocess running OK (PID: %s)", bot_process.pid)
        return True

    except Exception as e:
        bot_last_error = f"Failed to start bot: {str(e)}"
        logger.error(bot_last_error)
        return False

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
    """Quick check if supervisor manages the bot"""
    try:
        result = subprocess.run(
            ['supervisorctl', 'status', 'transport-bot'],
            capture_output=True, text=True, timeout=2
        )
        if 'RUNNING' in result.stdout:
            return True
    except Exception:
        pass
    try:
        result = subprocess.run(
            ['sudo', '-n', 'supervisorctl', 'status', 'transport-bot'],
            capture_output=True, text=True, timeout=2
        )
        if 'RUNNING' in result.stdout:
            return True
    except Exception:
        pass
    return False

@app.on_event("startup")
async def startup_event():
    logger.info("=== Backend startup ===")
    logger.info(f"BOT_DIR: {BOT_DIR}")
    logger.info(f"BOT_SCRIPT exists: {os.path.exists(BOT_SCRIPT)}")
    logger.info(f"Node binary: {find_node_binary()}")

    if is_supervisor_managing_bot():
        logger.info("Bot managed by supervisor - skipping subprocess start")
    else:
        logger.info("Supervisor not managing bot - starting as subprocess")
        result = start_bot_process()
        logger.info(f"Bot start result: {'SUCCESS' if result else 'FAILED'}")
        if bot_last_error:
            logger.error(f"Bot start error: {bot_last_error}")

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
                await client.get(ping_url, headers={"User-Agent": "TransportBot-KeepAlive/2.0"})
        except Exception:
            pass

async def bot_watchdog():
    """Check bot subprocess health every 30 seconds"""
    global bot_process
    while True:
        await asyncio.sleep(30)
        if is_supervisor_managing_bot():
            continue
        if bot_process is None or bot_process.poll() is not None:
            exit_code = bot_process.poll() if bot_process else 'N/A'
            logger.warning(f"Bot subprocess not running (exit: {exit_code}), restarting...")
            start_bot_process()

@app.on_event("shutdown")
async def shutdown_event():
    stop_bot_process()

@api_router.get("/")
async def root():
    return {"message": "Transport Monitor Bot API"}

@api_router.get("/bot/status")
async def get_bot_status():
    # First try the bot's internal status server
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(BOT_STATUS_URL)
            data = resp.json()
            data['subprocess'] = {
                'pid': bot_process.pid if bot_process else None,
                'running': bot_process.poll() is None if bot_process else False,
                'start_attempts': bot_start_attempts,
                'last_error': bot_last_error
            }
            return data
    except Exception:
        # Bot status server not responding - return diagnostic info
        return {
            "status": "offline",
            "subprocess": {
                'pid': bot_process.pid if bot_process else None,
                'running': bot_process.poll() is None if bot_process else False,
                'exit_code': bot_process.poll() if bot_process else None,
                'start_attempts': bot_start_attempts,
                'last_error': bot_last_error
            },
            "diagnostics": {
                'node_binary': find_node_binary(),
                'bot_script_exists': os.path.exists(BOT_SCRIPT),
                'config_exists': os.path.exists(CONFIG_PATH),
                'token_in_env': bool(os.environ.get('TELEGRAM_BOT_TOKEN')),
                'app_url': os.environ.get('APP_URL', 'not set')
            },
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

@api_router.get("/bot/diag")
async def bot_diagnostics():
    """Full diagnostics for debugging deployment issues"""
    node_bin = find_node_binary()
    node_version = None
    if node_bin:
        try:
            r = subprocess.run([node_bin, '--version'], capture_output=True, text=True, timeout=5)
            node_version = r.stdout.strip()
        except Exception:
            pass

    err_log_tail = ""
    try:
        if os.path.exists(BOT_ERR_FILE):
            r = subprocess.run(['tail', '-20', BOT_ERR_FILE], capture_output=True, text=True, timeout=5)
            err_log_tail = r.stdout
    except Exception:
        pass

    out_log_tail = ""
    try:
        if os.path.exists(BOT_LOG_FILE):
            r = subprocess.run(['tail', '-20', BOT_LOG_FILE], capture_output=True, text=True, timeout=5)
            out_log_tail = r.stdout
    except Exception:
        pass

    return {
        "node": {"binary": node_bin, "version": node_version},
        "bot_script": {"path": BOT_SCRIPT, "exists": os.path.exists(BOT_SCRIPT)},
        "config": {"path": CONFIG_PATH, "exists": os.path.exists(CONFIG_PATH)},
        "env": {
            "TELEGRAM_BOT_TOKEN": "set" if os.environ.get('TELEGRAM_BOT_TOKEN') else "NOT SET",
            "EMERGENT_LLM_KEY": "set" if os.environ.get('EMERGENT_LLM_KEY') else "NOT SET",
            "APP_URL": os.environ.get('APP_URL', 'NOT SET'),
            "NODE_ENV": os.environ.get('NODE_ENV', 'NOT SET'),
        },
        "subprocess": {
            "pid": bot_process.pid if bot_process else None,
            "running": bot_process.poll() is None if bot_process else False,
            "exit_code": bot_process.poll() if bot_process else None,
            "start_attempts": bot_start_attempts,
            "last_error": bot_last_error,
        },
        "logs": {
            "bot_log": out_log_tail[-1000:] if out_log_tail else "empty",
            "bot_err": err_log_tail[-1000:] if err_log_tail else "empty",
        },
        "bot_dir_contents": os.listdir(BOT_DIR) if os.path.exists(BOT_DIR) else "DIR NOT FOUND",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@api_router.post("/bot/restart")
async def restart_bot():
    global bot_process
    for cmd_prefix in [[], ['sudo', '-n']]:
        try:
            result = subprocess.run(
                cmd_prefix + ['supervisorctl', 'restart', 'transport-bot'],
                capture_output=True, text=True, timeout=5
            )
            if 'started' in result.stdout.lower():
                return {"success": True, "output": result.stdout.strip(), "method": "supervisor"}
        except Exception:
            continue
    stop_bot_process()
    ok = start_bot_process()
    return {"success": ok, "output": f"PID: {bot_process.pid if bot_process else 'N/A'}, error: {bot_last_error}", "method": "subprocess"}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
