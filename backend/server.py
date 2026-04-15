from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import httpx

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

app = FastAPI()
api_router = APIRouter(prefix="/api")

CONFIG_PATH = '/root/clawd/transport-bot-config.json'
STATE_PATH = '/root/clawd/transport-bot-state.json'
BOT_STATUS_URL = 'http://127.0.0.1:8099/health'

def read_json_file(filepath):
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception:
        return None

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
        safe_config['botTokenSet'] = bool(config.get('botToken'))
        safe_config['llmApiKeySet'] = bool(config.get('llmApiKey'))
        return safe_config
    return {"error": "Config not found"}

@api_router.get("/bot/logs")
async def get_bot_logs(lines: int = 50):
    logs = {"stdout": [], "stderr": []}
    for logtype, filepath in [("stdout", "/var/log/supervisor/transport-bot.out.log"), ("stderr", "/var/log/supervisor/transport-bot.err.log")]:
        try:
            result = subprocess.run(['tail', f'-{lines}', filepath], capture_output=True, text=True, timeout=5)
            logs[logtype] = result.stdout.strip().split('\n') if result.stdout.strip() else []
        except Exception:
            logs[logtype] = []
    return logs

@api_router.post("/bot/restart")
async def restart_bot():
    try:
        result = subprocess.run(['sudo', 'supervisorctl', 'restart', 'transport-bot'], capture_output=True, text=True, timeout=10)
        return {"success": True, "output": result.stdout.strip()}
    except Exception as e:
        return {"success": False, "error": str(e)}

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
