const https = require('https');
const http = require('http');
const fs = require('fs');
const path = require('path');

const CONFIG_PATH = path.join(__dirname, 'transport-bot-config.json');
const STATE_PATH = path.join(__dirname, 'transport-bot-state.json');
const DATA_DIR = __dirname;

let config = {};
let state = { lastUpdateId: 0, seenMessages: [], lastForwardTime: 0, stats: { totalProcessed: 0, totalForwarded: 0, totalBlocked: 0, startedAt: null } };

// ─── CONFIG & STATE ──────────────────────────────────────────

function loadConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    config = JSON.parse(raw);
    // Env overrides
    if (process.env.TELEGRAM_BOT_TOKEN) config.botToken = process.env.TELEGRAM_BOT_TOKEN;
    if (process.env.EMERGENT_LLM_KEY) config.llmApiKey = process.env.EMERGENT_LLM_KEY;
    log('Config loaded');
  } catch (err) {
    logError('Failed to load config', err);
    process.exit(1);
  }
}

function saveConfig() {
  try {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2), 'utf8');
    log('Config saved');
  } catch (err) {
    logError('Failed to save config', err);
  }
}

function loadState() {
  try {
    if (fs.existsSync(STATE_PATH)) {
      const raw = fs.readFileSync(STATE_PATH, 'utf8');
      state = JSON.parse(raw);
    }
    if (!state.stats) state.stats = { totalProcessed: 0, totalForwarded: 0, totalBlocked: 0, startedAt: null };
    state.stats.startedAt = new Date().toISOString();
    log(`State loaded. Last update ID: ${state.lastUpdateId}, Seen: ${state.seenMessages.length}`);
  } catch (err) {
    logError('Failed to load state', err);
  }
}

function saveState() {
  try {
    // Keep only last 1000 seen messages
    if (state.seenMessages.length > 1000) {
      state.seenMessages = state.seenMessages.slice(-1000);
    }
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), 'utf8');
  } catch (err) {
    logError('Failed to save state', err);
  }
}

// ─── LOGGING ────────────────────────────────────────────────

function log(msg) {
  if (config.debug) {
    console.log(`[${new Date().toISOString()}] ${msg}`);
  }
}

function logError(msg, err) {
  console.error(`[${new Date().toISOString()}] ERROR: ${msg}`, err ? err.message : '');
}

function logAlways(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

// ─── TELEGRAM API ───────────────────────────────────────────

function telegramAPI(method, params = {}) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(params);
    const options = {
      hostname: 'api.telegram.org',
      port: 443,
      path: `/bot${config.botToken}/${method}`,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      },
      timeout: method === 'getUpdates' ? 35000 : 15000
    };

    const req = https.request(options, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const parsed = JSON.parse(body);
          if (parsed.ok) {
            resolve(parsed.result);
          } else {
            reject(new Error(`Telegram API error: ${parsed.description || 'Unknown'}`));
          }
        } catch (e) {
          reject(new Error(`JSON parse error: ${e.message}`));
        }
      });
    });

    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Request timeout'));
    });

    req.write(data);
    req.end();
  });
}

// ─── LLM ANALYSIS ───────────────────────────────────────────

function analyzeLLM(text) {
  return new Promise((resolve, reject) => {
    const prompt = `Analyze if this message is about freight/transport/logistics. Reply with just "YES" or "NO".\n\nMessage: ${text.substring(0, 500)}`;

    const data = JSON.stringify({
      model: config.llmModel || 'claude-sonnet-4-5-20250929',
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 10
    });

    const options = {
      hostname: 'integrations.emergentagent.com',
      port: 443,
      path: '/llm/v1/messages',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': config.llmApiKey,
        'anthropic-version': '2023-06-01',
        'Content-Length': Buffer.byteLength(data)
      },
      timeout: 10000
    };

    const req = https.request(options, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          const parsed = JSON.parse(body);
          const content = parsed.content?.[0]?.text || '';
          resolve(content.toUpperCase().includes('YES'));
        } catch (e) {
          reject(new Error(`LLM parse error: ${e.message}`));
        }
      });
    });

    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('LLM timeout'));
    });

    req.write(data);
    req.end();
  });
}

// ─── MESSAGE FILTERING ──────────────────────────────────────

function scoreMessage(text) {
  const lower = text.toLowerCase();

  // Check negative keywords first
  for (const neg of config.negKeywords) {
    if (lower.includes(neg.toLowerCase())) {
      return { score: -1, blocked: true, matchedNeg: neg };
    }
  }

  // Score positive keywords
  let score = 0;
  const matched = [];
  for (const kw of config.keywords) {
    if (lower.includes(kw.toLowerCase())) {
      score++;
      matched.push(kw);
    }
  }

  return { score, blocked: false, matched };
}

// ─── MESSAGE FORWARDING ─────────────────────────────────────

async function forwardMessage(msg, score, llmResult, matched) {
  const chatTitle = msg.chat.title || msg.chat.username || `ID:${msg.chat.id}`;
  const fromUser = msg.from
    ? (msg.from.username ? `@${msg.from.username}` : `${msg.from.first_name || ''} ${msg.from.last_name || ''}`.trim())
    : 'Unknown';
  const text = msg.text || msg.caption || '';

  const llmStatus = config.useLLM ? (llmResult ? 'YES' : 'NO') : 'OFF';

  const forwardText = [
    '\u{1F697} Новый запрос',
    '',
    `\u{1F4CD} Группа: ${chatTitle}`,
    `\u{1F464} От: ${fromUser}`,
    `\u{1F4CA} Score: ${score} | LLM: ${llmStatus}`,
    matched && matched.length > 0 ? `\u{1F50D} Слова: ${matched.join(', ')}` : '',
    '',
    text.substring(0, 3000)
  ].filter(Boolean).join('\n');

  const recipients = config.destUserIds || [];
  let sent = 0;

  for (const recipientId of recipients) {
    try {
      // Rate limiting
      const now = Date.now();
      const elapsed = now - state.lastForwardTime;
      if (elapsed < (config.rateLimit || 1000)) {
        await sleep(config.rateLimit - elapsed);
      }

      await telegramAPI('sendMessage', {
        chat_id: recipientId,
        text: forwardText,
        disable_web_page_preview: true
      });

      state.lastForwardTime = Date.now();
      sent++;
      log(`Forwarded to ${recipientId}`);
    } catch (err) {
      logError(`Failed to forward to ${recipientId}`, err);
    }
  }

  state.stats.totalForwarded++;
  return sent;
}

// ─── COMMAND HANDLING ───────────────────────────────────────

function isOwner(userId) {
  const id = String(userId);
  return (config.destUserIds || []).includes(id);
}

async function handleCommand(msg) {
  const userId = msg.from?.id;
  if (!userId || !isOwner(userId)) return false;

  const text = (msg.text || '').trim();
  if (!text.startsWith('/')) return false;

  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase().replace(/@\w+$/, ''); // Remove @botname
  const arg = parts[1] || '';

  let reply = '';

  switch (cmd) {
    case '/start':
      reply = [
        '\u{1F697} Transport Monitor Bot',
        '',
        'Команды:',
        '/status - Текущая конфигурация',
        '/threshold <n> - Порог (1-10)',
        '/llm on|off - LLM анализ',
        '/debug on|off - Debug логи',
        '/addchat <id> - Добавить группу',
        '/rmchat <id> - Удалить группу'
      ].join('\n');
      break;

    case '/status':
      reply = [
        '\u{1F4CA} Status:',
        '',
        `\u{1F527} Mode: ${config.useLLM ? 'Keywords + LLM' : 'Keywords only'}`,
        `\u{1F4CB} Allowed chats: ${config.allowedChats?.length ? config.allowedChats.join(', ') : 'ALL'}`,
        `\u{1F3AF} Threshold: ${config.threshold}`,
        `\u{1F511} Keywords: ${config.keywords?.length || 0}`,
        `\u{1F6AB} Negative: ${config.negKeywords?.length || 0}`,
        `\u{1F465} Recipients: ${config.destUserIds?.length || 0} (${(config.destUserIds || []).join(', ')})`,
        `\u{1F4BE} Seen: ${state.seenMessages?.length || 0} messages`,
        `\u{1F50D} Debug: ${config.debug ? 'ON' : 'OFF'}`,
        `\u{1F9E0} LLM: ${config.useLLM ? 'ON' : 'OFF'}`,
        '',
        `\u{1F4C8} Stats:`,
        `  Processed: ${state.stats?.totalProcessed || 0}`,
        `  Forwarded: ${state.stats?.totalForwarded || 0}`,
        `  Blocked: ${state.stats?.totalBlocked || 0}`,
        `  Running since: ${state.stats?.startedAt || 'unknown'}`
      ].join('\n');
      break;

    case '/threshold':
      const n = parseInt(arg);
      if (n >= 1 && n <= 10) {
        config.threshold = n;
        saveConfig();
        reply = `\u{2705} Threshold set to ${n}`;
      } else {
        reply = '\u{274C} Usage: /threshold <1-10>';
      }
      break;

    case '/llm':
      if (arg === 'on') {
        config.useLLM = true;
        saveConfig();
        reply = '\u{2705} LLM analysis enabled';
      } else if (arg === 'off') {
        config.useLLM = false;
        saveConfig();
        reply = '\u{2705} LLM analysis disabled';
      } else {
        reply = '\u{274C} Usage: /llm on|off';
      }
      break;

    case '/debug':
      if (arg === 'on') {
        config.debug = true;
        saveConfig();
        reply = '\u{2705} Debug mode enabled';
      } else if (arg === 'off') {
        config.debug = false;
        saveConfig();
        reply = '\u{2705} Debug mode disabled';
      } else {
        reply = '\u{274C} Usage: /debug on|off';
      }
      break;

    case '/addchat':
      if (arg) {
        const chatId = arg.startsWith('-') ? arg : `-${arg}`;
        if (!config.allowedChats) config.allowedChats = [];
        if (!config.allowedChats.includes(chatId)) {
          config.allowedChats.push(chatId);
          saveConfig();
          reply = `\u{2705} Chat ${chatId} added to whitelist`;
        } else {
          reply = `\u{2139}\u{FE0F} Chat ${chatId} already in whitelist`;
        }
      } else {
        reply = '\u{274C} Usage: /addchat <chat_id>';
      }
      break;

    case '/rmchat':
      if (arg) {
        const chatId = arg.startsWith('-') ? arg : `-${arg}`;
        if (config.allowedChats) {
          config.allowedChats = config.allowedChats.filter(c => c !== chatId);
          saveConfig();
          reply = `\u{2705} Chat ${chatId} removed from whitelist`;
        }
      } else {
        reply = '\u{274C} Usage: /rmchat <chat_id>';
      }
      break;

    default:
      return false;
  }

  if (reply) {
    try {
      await telegramAPI('sendMessage', {
        chat_id: msg.chat.id,
        text: reply
      });
    } catch (err) {
      logError('Failed to send command reply', err);
    }
  }

  return true;
}

// ─── MAIN POLLING LOOP ──────────────────────────────────────

async function processUpdate(update) {
  const msg = update.message || update.channel_post;
  if (!msg) return;

  // Get text
  const text = msg.text || msg.caption || '';
  if (!text) return;

  // Deduplication
  const msgKey = `${msg.chat.id}:${msg.message_id}`;
  if (state.seenMessages.includes(msgKey)) return;
  state.seenMessages.push(msgKey);

  // Check if it's a command (private chat or direct mention)
  if (msg.chat.type === 'private') {
    const handled = await handleCommand(msg);
    if (handled) return;
  }

  // Check allowed chats
  if (config.allowedChats && config.allowedChats.length > 0) {
    const chatId = String(msg.chat.id);
    if (!config.allowedChats.includes(chatId)) {
      log(`Skipping message from non-whitelisted chat: ${chatId}`);
      return;
    }
  }

  // Skip private messages that aren't commands
  if (msg.chat.type === 'private') return;

  state.stats.totalProcessed++;

  // Score the message
  const result = scoreMessage(text);

  if (result.blocked) {
    state.stats.totalBlocked++;
    log(`BLOCKED: neg keyword "${result.matchedNeg}" in: ${text.substring(0, 80)}`);
    return;
  }

  log(`Score: ${result.score}/${config.threshold} for: ${text.substring(0, 80)}... [${(result.matched || []).join(', ')}]`);

  if (result.score < config.threshold) return;

  // Optional LLM check
  let llmResult = null;
  if (config.useLLM) {
    try {
      llmResult = await analyzeLLM(text);
      log(`LLM result: ${llmResult ? 'YES' : 'NO'}`);
      if (!llmResult) {
        log('LLM rejected message, skipping');
        return;
      }
    } catch (err) {
      logError('LLM analysis failed, falling back to keywords', err);
      llmResult = null; // Fallback - use keyword score only
    }
  }

  // Forward
  await forwardMessage(msg, result.score, llmResult, result.matched);
}

async function poll() {
  log('Polling for updates...');

  const updates = await telegramAPI('getUpdates', {
    offset: state.lastUpdateId + 1,
    timeout: 30,
    allowed_updates: ['message', 'channel_post']
  });

  if (updates && updates.length > 0) {
    log(`Received ${updates.length} update(s)`);

    for (const update of updates) {
      state.lastUpdateId = update.update_id;
      try {
        await processUpdate(update);
      } catch (err) {
        logError('Error processing update', err);
      }
    }

    saveState();
  }
}

// ─── HTTP STATUS SERVER ─────────────────────────────────────

function startStatusServer() {
  const port = 8099;
  const server = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET');

    if (req.url === '/health' || req.url === '/bot-status') {
      const statusData = {
        status: 'running',
        uptime: state.stats.startedAt ? Math.floor((Date.now() - new Date(state.stats.startedAt).getTime()) / 1000) : 0,
        config: {
          useLLM: config.useLLM,
          debug: config.debug,
          threshold: config.threshold,
          keywordsCount: config.keywords?.length || 0,
          negKeywordsCount: config.negKeywords?.length || 0,
          recipientsCount: config.destUserIds?.length || 0,
          allowedChats: config.allowedChats?.length ? config.allowedChats : 'ALL',
          botTokenSet: !!config.botToken,
          llmApiKeySet: !!config.llmApiKey
        },
        stats: state.stats,
        seenMessages: state.seenMessages?.length || 0,
        lastUpdateId: state.lastUpdateId,
        timestamp: new Date().toISOString()
      };
      res.writeHead(200);
      res.end(JSON.stringify(statusData));
    } else {
      res.writeHead(404);
      res.end(JSON.stringify({ error: 'Not found' }));
    }
  });

  server.listen(port, '127.0.0.1', () => {
    logAlways(`Status server running on http://127.0.0.1:${port}`);
  });
}

// ─── KEEP-ALIVE PING ────────────────────────────────────────

function startKeepAlive() {
  // Get the app URL from env (set by Emergent on deploy)
  const appUrl = process.env.APP_URL || process.env.REACT_APP_BACKEND_URL || '';
  if (!appUrl) {
    logAlways('No APP_URL set, keep-alive disabled');
    return;
  }

  const pingUrl = appUrl.replace(/\/$/, '') + '/api/';
  logAlways(`Keep-alive ping target: ${pingUrl}`);

  // Ping every 4 minutes to prevent pod from sleeping
  setInterval(() => {
    const url = new URL(pingUrl);
    const options = {
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname,
      method: 'GET',
      timeout: 10000
    };

    const transport = url.protocol === 'https:' ? https : http;
    const req = transport.request(options, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        log(`Keep-alive ping OK (${res.statusCode})`);
      });
    });
    req.on('error', (err) => {
      log(`Keep-alive ping failed: ${err.message}`);
    });
    req.on('timeout', () => { req.destroy(); });
    req.end();
  }, 4 * 60 * 1000); // every 4 minutes
}

// ─── UTILITIES ──────────────────────────────────────────────

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ─── AUTO-RECOVERY MAIN LOOP ────────────────────────────────

async function startWithRecovery() {
  loadConfig();
  loadState();
  startStatusServer();
  startKeepAlive();

  logAlways('Transport Monitor Bot starting...');
  logAlways(`Bot token: ${config.botToken ? config.botToken.substring(0, 10) + '...' : 'MISSING'}`);
  logAlways(`Recipients: ${(config.destUserIds || []).join(', ')}`);
  logAlways(`Keywords: ${config.keywords?.length || 0} positive, ${config.negKeywords?.length || 0} negative`);
  logAlways(`LLM: ${config.useLLM ? 'ON' : 'OFF'}`);
  logAlways(`Threshold: ${config.threshold}`);

  // Send startup notification
  try {
    for (const uid of (config.destUserIds || [])) {
      await telegramAPI('sendMessage', {
        chat_id: uid,
        text: '\u{2705} Transport Monitor Bot started!\n\nSend /status to check configuration.\nSend /start for list of commands.'
      });
    }
  } catch (err) {
    logError('Failed to send startup notification', err);
  }

  while (true) {
    try {
      await poll();
    } catch (err) {
      logError('Fatal error, restarting in 10 seconds', err);
      await sleep(10000);
      logAlways('Restarting bot...');
    }
  }
}

startWithRecovery();
