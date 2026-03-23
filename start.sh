#!/bin/bash
# 🦞 龙虾 Agent 军团 — 一键启动/停止
# Usage: ./start.sh [start|stop|restart|status]

BASE="$(cd "$(dirname "$0")" && pwd)"
PY="/home/user/miniforge3/bin/python3"
FE="$BASE/frontend"
LOG="$BASE/logs"
PIDF="$BASE/.pids"

export RESEARCHCLAW_API_KEY="${RESEARCHCLAW_API_KEY:-sk-QLo52KgqSRHiI3H3JydKzJJJw4W0URzsNnGy8d3QB1yYtFqM}"

FNM_DIR="${FNM_DIR:-$HOME/.local/share/fnm}"
export PATH="$FNM_DIR:$PATH"
eval "$($FNM_DIR/fnm env 2>/dev/null)" 2>/dev/null

mkdir -p "$LOG" "$PIDF"

# Ascend NPU CANN environment (no-op if not installed)
source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null

G='\033[0;32m'; R='\033[0;31m'; Y='\033[0;33m'; N='\033[0m'

IDEA_COUNT=0
IDEA_TOPIC=""
IDEA_CONFIG=""

do_start() {
    echo "🦞 启动龙虾 Agent 军团..."
    echo ""

    # 1) Resource Monitor
    if ss -tlnp 2>/dev/null | grep -q ":8895 "; then
        echo -e "  ${Y}⏭ resource_monitor 已在运行${N}"
    else
        nohup $PY -u "$BASE/backend/services/resource_monitor.py" --port 8895 \
            > "$LOG/resource_monitor.log" 2>&1 &
        echo $! > "$PIDF/resource_monitor.pid"
        sleep 1
        echo -e "  ${G}✅ resource_monitor (PID=$!)${N}"
    fi

    # 2) Agent Bridge
    if ss -tlnp 2>/dev/null | grep -q ":8896 "; then
        echo -e "  ${Y}⏭ agent_bridge 已在运行${N}"
    else
        LLM_CFG=""
        for _cfg in "$BASE/backend/agent/config_gpu_project.yaml" \
                     "$BASE/backend/agent/config.researchclaw.yaml"; do
            [ -f "$_cfg" ] && LLM_CFG="--llm-config $_cfg" && break
        done
        nohup $PY -u "$BASE/backend/services/agent_bridge.py" \
            --port 8896 --python "$PY" \
            --agent-dir "$BASE/backend/agent" \
            --runs-dir "$BASE/backend/runs" \
            --pool-idea 3 --pool-exp 2 --pool-code 3 --pool-exec 4 --pool-write 2 \
            --total-gpus 8 --gpus-per-project 1 \
            --discussion-mode --discussion-rounds 2 \
            --discussion-models "claude-opus-4-6,claude-opus-4-5-20251101" \
            ${AUTO_LOOP:+--auto-loop} \
            ${IDEA_COUNT:+--idea-count $IDEA_COUNT} \
            ${IDEA_TOPIC:+--idea-topic "$IDEA_TOPIC"} \
            ${IDEA_CONFIG:+--idea-config "$IDEA_CONFIG"} \
            > "$LOG/agent_bridge.log" 2>&1 &
        echo $! > "$PIDF/agent_bridge.pid"
        sleep 1
        echo -e "  ${G}✅ agent_bridge (PID=$!)${N}"
    fi

    # 3) Frontend Vite
    if ss -tlnp 2>/dev/null | grep -q ":5893 "; then
        echo -e "  ${Y}⏭ frontend 已在运行${N}"
    else
        cd "$FE"
        nohup npx vite --host 0.0.0.0 --port 5893 \
            > "$LOG/frontend.log" 2>&1 &
        echo $! > "$PIDF/frontend.pid"
        sleep 2
        echo -e "  ${G}✅ frontend (PID=$!)${N}"
        cd "$BASE"
    fi

    echo ""
    echo "📍 服务地址:"
    echo -e "   ${G}前端 UI:      http://localhost:5893/${N}"
    echo "   资源监控 WS:  ws://localhost:8895"
    echo "   Agent Bridge: ws://localhost:8896"
    echo ""
}

do_stop() {
    echo "🛑 停止所有服务..."
    for svc in frontend agent_bridge resource_monitor; do
        f="$PIDF/$svc.pid"
        if [ -f "$f" ]; then
            pid=$(cat "$f")
            # Verify the PID actually belongs to our service before killing
            if kill -0 "$pid" 2>/dev/null; then
                proc_cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
                case "$proc_cmd" in
                    *resource_monitor*|*agent_bridge*|*vite*|*npm*)
                        kill "$pid" 2>/dev/null && sleep 0.5
                        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
                        echo -e "  ${G}⏹ $svc (PID=$pid)${N}"
                        ;;
                    *)
                        echo -e "  ${Y}⏭ $svc PID=$pid belongs to another process, skipping${N}"
                        ;;
                esac
            fi
            rm -f "$f"
        fi
    done
    # Also kill by port in case PID file was stale
    for port in 5893 8895 8896; do
        lsof -ti:$port 2>/dev/null | xargs -r kill -9 2>/dev/null
    done
    echo ""
}

do_status() {
    echo "📊 服务状态:"
    for pair in "resource_monitor:8895" "agent_bridge:8896" "frontend:5893"; do
        svc="${pair%%:*}"; port="${pair##*:}"
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            echo -e "  ${G}● $svc${N} (port $port)"
        else
            echo -e "  ${R}○ $svc${N} (port $port)"
        fi
    done
    echo ""
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)  do_status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" ;;
esac
