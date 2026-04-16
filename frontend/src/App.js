import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Activity, RefreshCw, Clock, Send, ShieldCheck, ShieldX,
  Terminal, Settings, Zap, MessageSquare, AlertTriangle
} from 'lucide-react';
import './App.css';

const API = process.env.REACT_APP_BACKEND_URL;

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '0s';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts = [];
  if (d > 0) parts.push(`${d}d`);
  if (h > 0) parts.push(`${h}h`);
  if (m > 0) parts.push(`${m}m`);
  if (s > 0 || parts.length === 0) parts.push(`${s}s`);
  return parts.join(' ');
}

function parseLogLevel(line) {
  if (line.includes('ERROR')) return 'error';
  if (line.includes('WARNING') || line.includes('WARN')) return 'warn';
  return 'info';
}

function StatusBadge({ online }) {
  return (
    <span
      data-testid="bot-status-indicator"
      className="font-mono text-xs uppercase tracking-widest font-bold px-2.5 py-0.5 inline-flex items-center gap-1.5"
      style={{
        background: online ? 'rgba(0,163,129,0.15)' : 'rgba(229,72,77,0.15)',
        color: online ? 'var(--success)' : 'var(--destructive)',
        border: `1px solid ${online ? 'rgba(0,163,129,0.3)' : 'rgba(229,72,77,0.3)'}`
      }}
    >
      <span className={`w-2 h-2 rounded-full ${online ? 'bg-[var(--success)] animate-pulse' : 'bg-[var(--destructive)]'}`} />
      {online ? 'ONLINE' : 'OFFLINE'}
    </span>
  );
}

function MetricCard({ testId, label, value, icon: Icon, color }) {
  return (
    <div className="bg-white p-6 flex flex-col gap-3">
      <div className="flex flex-row items-center justify-between pb-2 border-b" style={{ borderColor: 'rgba(228,228,231,0.4)' }}>
        <span className="text-xs uppercase tracking-[0.2em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>{label}</span>
        <Icon className="w-4 h-4" style={{ color: color || 'var(--muted-foreground)' }} strokeWidth={1.5} />
      </div>
      <span data-testid={testId} className="font-mono text-5xl font-medium tracking-tighter" style={{ color: color || 'var(--foreground)' }}>
        {value}
      </span>
    </div>
  );
}

function ConfigPanel({ config }) {
  if (!config) return null;

  const items = [
    { label: 'MODE', value: config.useLLM ? 'Keywords + LLM' : 'Keywords Only' },
    { label: 'THRESHOLD', value: config.threshold || 1 },
    { label: 'KEYWORDS', value: config.keywordsCount || 0 },
    { label: 'NEG KEYWORDS', value: config.negKeywordsCount || 0 },
    { label: 'RECIPIENTS', value: config.recipientsCount || 0 },
    { label: 'CHATS', value: config.allowedChats === 'ALL' ? 'ALL' : `${config.allowedChats?.length || 0} whitelisted` },
    { label: 'DEBUG', value: config.debug ? 'ON' : 'OFF' },
    { label: 'LLM', value: config.useLLM ? 'ON' : 'OFF' },
    { label: 'BOT TOKEN', value: config.botTokenSet ? 'Set' : 'Missing' },
    { label: 'LLM KEY', value: config.llmApiKeySet ? 'Set' : 'Missing' },
  ];

  return (
    <div data-testid="config-panel" className="bg-white p-6 flex flex-col gap-4 h-full">
      <div className="flex flex-row items-center justify-between pb-2 border-b" style={{ borderColor: 'rgba(228,228,231,0.4)' }}>
        <span className="text-xs uppercase tracking-[0.2em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>Configuration</span>
        <Settings className="w-4 h-4" style={{ color: 'var(--muted-foreground)' }} strokeWidth={1.5} />
      </div>
      <div className="flex flex-col gap-0 flex-1">
        {items.map((item, i) => (
          <div key={i} className="flex justify-between items-center py-2.5 border-b last:border-b-0" style={{ borderColor: 'rgba(228,228,231,0.3)' }}>
            <span className="text-xs uppercase tracking-[0.15em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>{item.label}</span>
            <span className="font-mono text-sm font-medium">{String(item.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function LogViewer({ logs }) {
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  const allLogs = [...(logs?.stdout || []), ...(logs?.stderr || [])].filter(Boolean);

  return (
    <div data-testid="recent-logs-viewer" className="bg-white p-0 flex flex-col h-full">
      <div className="flex flex-row items-center justify-between p-6 pb-2 border-b" style={{ borderColor: 'rgba(228,228,231,0.4)' }}>
        <span className="text-xs uppercase tracking-[0.2em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>Recent Logs</span>
        <Terminal className="w-4 h-4" style={{ color: 'var(--muted-foreground)' }} strokeWidth={1.5} />
      </div>
      <div
        ref={scrollRef}
        className="font-mono text-sm p-4 overflow-y-auto overflow-x-hidden flex-1"
        style={{
          background: 'var(--terminal-bg)',
          color: 'var(--terminal-fg)',
          minHeight: '400px',
          maxHeight: '500px'
        }}
      >
        {allLogs.length === 0 ? (
          <div className="flex items-center justify-center h-full" style={{ color: '#71717a' }}>
            No logs available
          </div>
        ) : (
          allLogs.map((line, i) => {
            const level = parseLogLevel(line);
            const levelColors = {
              info: '#60a5fa',
              warn: '#facc15',
              error: '#f87171'
            };
            const levelLabels = { info: 'INFO', warn: 'WARN', error: 'ERR ' };
            // Try to extract timestamp
            const tsMatch = line.match(/^\[([^\]]+)\]/);
            const timestamp = tsMatch ? tsMatch[1].substring(11, 19) : '';
            const message = tsMatch ? line.substring(tsMatch[0].length).trim() : line;

            return (
              <div key={i} className="flex gap-3 py-1" style={{ ':hover': { background: 'rgba(255,255,255,0.05)' } }}>
                {timestamp && <span style={{ color: '#71717a', flexShrink: 0 }}>{timestamp}</span>}
                <span style={{ color: levelColors[level], flexShrink: 0, width: '3rem' }}>{levelLabels[level]}</span>
                <span style={{ color: '#d4d4d8', wordBreak: 'break-all' }}>{message}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function ConfirmDialog({ open, onConfirm, onCancel, loading }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: 'rgba(0,0,0,0.5)' }}>
      <div data-testid="restart-confirm-dialog" className="bg-white p-8 max-w-md w-full mx-4 border" style={{ borderColor: 'var(--border)' }}>
        <div className="flex items-center gap-3 mb-4">
          <AlertTriangle className="w-5 h-5" style={{ color: 'var(--destructive)' }} strokeWidth={1.5} />
          <h3 className="text-xl tracking-tight font-medium">Restart Bot?</h3>
        </div>
        <p className="mb-6" style={{ color: 'var(--muted-foreground)' }}>
          This will restart the Transport Monitor Bot. It may take a few seconds to come back online.
        </p>
        <div className="flex gap-3 justify-end">
          <button
            data-testid="restart-cancel-button"
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium border transition-all hover:bg-[var(--accent)]"
            style={{ borderColor: 'var(--input)', background: 'var(--background)' }}
          >
            Cancel
          </button>
          <button
            data-testid="restart-confirm-button"
            onClick={onConfirm}
            disabled={loading}
            className="px-4 py-2 text-sm font-medium transition-all flex items-center gap-2"
            style={{ background: 'var(--destructive)', color: 'var(--destructive-foreground)' }}
          >
            {loading && <RefreshCw className="w-4 h-4 animate-spin" strokeWidth={1.5} />}
            Restart
          </button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState(null);
  const [logs, setLogs] = useState(null);
  const [loading, setLoading] = useState(true);
  const [restartDialog, setRestartDialog] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, configRes, logsRes] = await Promise.all([
        fetch(`${API}/api/bot/status`).then(r => r.json()).catch(() => ({ status: 'offline' })),
        fetch(`${API}/api/bot/config`).then(r => r.json()).catch(() => null),
        fetch(`${API}/api/bot/logs?lines=80`).then(r => r.json()).catch(() => null)
      ]);
      setStatus(statusRes);
      setConfig(configRes);
      setLogs(logsRes);
      setLastRefresh(new Date());
    } catch (err) {
      console.error('Fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleRestart = async () => {
    setRestarting(true);
    try {
      await fetch(`${API}/api/bot/restart`, { method: 'POST' });
      setRestartDialog(false);
      setTimeout(fetchData, 3000);
    } catch (err) {
      console.error('Restart error:', err);
    } finally {
      setRestarting(false);
    }
  };

  const isOnline = status?.status === 'running';
  const subprocess = status?.subprocess;
  const diagnostics = status?.diagnostics;

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: 'var(--secondary)' }}>
        <div className="flex items-center gap-3">
          <RefreshCw className="w-5 h-5 animate-spin" style={{ color: 'var(--primary)' }} strokeWidth={1.5} />
          <span className="font-mono text-sm uppercase tracking-widest" style={{ color: 'var(--muted-foreground)' }}>Loading...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4 md:p-8" style={{ background: 'var(--secondary)' }}>
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-6 gap-4">
        <div className="flex items-center gap-4">
          <h1 className="text-3xl tracking-tight font-bold" style={{ fontFamily: 'IBM Plex Sans, sans-serif' }}>
            Transport Monitor
          </h1>
          <StatusBadge online={isOnline} />
        </div>
        <div className="flex items-center gap-3">
          {lastRefresh && (
            <span className="font-mono text-xs" style={{ color: 'var(--muted-foreground)' }}>
              Updated {lastRefresh.toLocaleTimeString()}
            </span>
          )}
          <button
            data-testid="refresh-button"
            onClick={() => { setLoading(true); fetchData(); }}
            className="px-3 py-2 text-sm font-medium border transition-all hover:bg-[var(--accent)] flex items-center gap-2"
            style={{ borderColor: 'var(--input)', background: 'var(--background)' }}
          >
            <RefreshCw className="w-4 h-4" strokeWidth={1.5} />
            Refresh
          </button>
          <button
            data-testid="restart-bot-button"
            onClick={() => setRestartDialog(true)}
            className="px-3 py-2 text-sm font-medium transition-all hover:opacity-90 flex items-center gap-2"
            style={{ background: 'var(--destructive)', color: 'var(--destructive-foreground)' }}
          >
            <Zap className="w-4 h-4" strokeWidth={1.5} />
            Restart Bot
          </button>
        </div>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-px border" style={{ background: 'rgba(228,228,231,0.6)', borderColor: 'rgba(228,228,231,0.6)' }}>
        {/* Row 1: Status + Uptime + Metrics */}
        <div className="lg:col-span-3">
          <div className="bg-white p-6 flex flex-col gap-3 h-full">
            <div className="flex flex-row items-center justify-between pb-2 border-b" style={{ borderColor: 'rgba(228,228,231,0.4)' }}>
              <span className="text-xs uppercase tracking-[0.2em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>Status</span>
              <Activity className="w-4 h-4" style={{ color: isOnline ? 'var(--success)' : 'var(--destructive)' }} strokeWidth={1.5} />
            </div>
            <StatusBadge online={isOnline} />
            <div className="mt-auto pt-2 flex flex-col gap-1">
              <span className="font-mono text-xs" style={{ color: 'var(--muted-foreground)' }}>
                Last Update ID: {status?.lastUpdateId || 0}
              </span>
              {subprocess && !isOnline && (
                <span className="font-mono text-xs" style={{ color: 'var(--destructive)' }}>
                  {subprocess.last_error ? subprocess.last_error.substring(0, 80) : `PID: ${subprocess.pid || 'none'}, attempts: ${subprocess.start_attempts || 0}`}
                </span>
              )}
              {diagnostics && !isOnline && (
                <span className="font-mono text-xs" style={{ color: 'var(--muted-foreground)' }}>
                  node: {diagnostics.node_binary ? 'OK' : 'MISSING'} | token: {diagnostics.token_in_env ? 'OK' : 'MISSING'}
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="lg:col-span-3">
          <div className="bg-white p-6 flex flex-col gap-3 h-full">
            <div className="flex flex-row items-center justify-between pb-2 border-b" style={{ borderColor: 'rgba(228,228,231,0.4)' }}>
              <span className="text-xs uppercase tracking-[0.2em] font-semibold" style={{ color: 'var(--muted-foreground)' }}>Uptime</span>
              <Clock className="w-4 h-4" style={{ color: 'var(--muted-foreground)' }} strokeWidth={1.5} />
            </div>
            <span data-testid="uptime-display" className="font-mono text-4xl font-medium tracking-tighter">
              {formatUptime(status?.uptime || 0)}
            </span>
            <span className="font-mono text-xs mt-auto" style={{ color: 'var(--muted-foreground)' }}>
              Seen: {status?.seenMessages || 0} msgs
            </span>
          </div>
        </div>

        <div className="lg:col-span-2">
          <MetricCard
            testId="processed-metric"
            label="Processed"
            value={status?.stats?.totalProcessed || 0}
            icon={MessageSquare}
            color="var(--primary)"
          />
        </div>

        <div className="lg:col-span-2">
          <MetricCard
            testId="forwarded-metric"
            label="Forwarded"
            value={status?.stats?.totalForwarded || 0}
            icon={Send}
            color="var(--success)"
          />
        </div>

        <div className="lg:col-span-2">
          <MetricCard
            testId="blocked-metric"
            label="Blocked"
            value={status?.stats?.totalBlocked || 0}
            icon={ShieldX}
            color="var(--destructive)"
          />
        </div>

        {/* Row 2: Logs (8 cols) + Config (4 cols) */}
        <div className="lg:col-span-8 lg:row-span-2">
          <LogViewer logs={logs} />
        </div>

        <div className="lg:col-span-4 lg:row-span-2">
          <ConfigPanel config={status?.config || config} />
        </div>
      </div>

      {/* Footer */}
      <div className="mt-4 flex items-center justify-between">
        <span className="font-mono text-xs" style={{ color: 'var(--muted-foreground)' }}>
          Transport Monitor Bot v2.0
        </span>
        <span className="font-mono text-xs" style={{ color: 'var(--muted-foreground)' }}>
          Auto-refresh: 10s
        </span>
      </div>

      <ConfirmDialog
        open={restartDialog}
        onConfirm={handleRestart}
        onCancel={() => setRestartDialog(false)}
        loading={restarting}
      />
    </div>
  );
}

export default App;
