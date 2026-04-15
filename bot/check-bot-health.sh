#!/bin/bash

echo "=== Transport Bot Health Check ==="
echo ""

# Check process
PID=$(ps aux | grep "transport-monitor-bot" | grep -v grep | awk '{print $2}')
if [ -z "$PID" ]; then
    echo "PROCESS: NOT running!"
    exit 1
else
    echo "PROCESS: Running (PID: $PID)"
fi

# Check supervisor
SUPERVISOR_STATUS=$(sudo supervisorctl status transport-bot 2>/dev/null | grep RUNNING)
if [ -z "$SUPERVISOR_STATUS" ]; then
    echo "SUPERVISOR: NOT running!"
else
    echo "SUPERVISOR: Running"
fi

# Check HTTP status server
HTTP_RESPONSE=$(curl -s http://127.0.0.1:8099/health 2>/dev/null)
if [ -z "$HTTP_RESPONSE" ]; then
    echo "HTTP STATUS: NOT responding!"
else
    echo "HTTP STATUS: OK"
    echo "$HTTP_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Uptime: {d.get('uptime',0)}s\"); print(f\"  Processed: {d.get('stats',{}).get('totalProcessed',0)}\"); print(f\"  Forwarded: {d.get('stats',{}).get('totalForwarded',0)}\"); print(f\"  Blocked: {d.get('stats',{}).get('totalBlocked',0)}\")" 2>/dev/null
fi

# Check recent activity from logs
for LOG_PATH in /var/log/supervisor/transport-bot.err.log /tmp/transport-bot.err.log; do
    if [ -f "$LOG_PATH" ]; then
        RECENT_ERRORS=$(tail -100 "$LOG_PATH" 2>/dev/null | grep -i "ERROR" | wc -l)
        if [ "$RECENT_ERRORS" -gt 0 ]; then
            echo "ERRORS: Found $RECENT_ERRORS recent errors in $LOG_PATH"
        else
            echo "ERRORS: None in $LOG_PATH"
        fi
        break
    fi
done

echo ""
echo "Bot HEALTHY"
exit 0
