#!/usr/bin/env bash
# Cold-to-hot startup for submissions-agent.
# Starts Ollama, (optionally) Jaeger, and the web server.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ── colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }

# ── flags ────────────────────────────────────────────────────────────────────
JAEGER=false
OPEN_BROWSER=false
PORT=8765

while [[ $# -gt 0 ]]; do
    case "$1" in
        --jaeger)        JAEGER=true ;;
        --open)          OPEN_BROWSER=true ;;
        --port)          PORT="$2"; shift ;;
        -h|--help)
            echo "Usage: start.sh [--jaeger] [--open] [--port N]"
            echo "  --jaeger   also start Jaeger all-in-one (requires Docker)"
            echo "  --open     open http://localhost:$PORT in browser when ready"
            echo "  --port N   web server port (default 8765)"
            exit 0 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
    shift
done

echo "submissions-agent — startup"
echo "==========================="

# ── pyenv ────────────────────────────────────────────────────────────────────
if command -v pyenv &>/dev/null; then
    eval "$(pyenv init -)" 2>/dev/null || true
fi

# ── 1. verify .env ───────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    warn ".env missing — run ./scripts/setup.sh first"
    exit 1
fi

# ── 2. verify package installed ──────────────────────────────────────────────
if ! command -v web-server &>/dev/null; then
    info "Package not installed — running pip install -e ."
    python3 -m pip install -e . --quiet
    ok "Package installed"
fi

# ── 3. Ollama ────────────────────────────────────────────────────────────────
OLLAMA_BIN=$(command -v ollama \
    || ls /Users/"$USER"/Applications/Ollama.app/Contents/Resources/ollama 2>/dev/null \
    || ls /Applications/Ollama.app/Contents/Resources/ollama 2>/dev/null \
    || echo "")

if [[ -z "$OLLAMA_BIN" ]]; then
    warn "ollama not found — agent --backend ollama will not work"
else
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        ok "Ollama already running"
    else
        info "Starting Ollama..."
        "$OLLAMA_BIN" serve &>/tmp/ollama.log 2>&1 &
        OLLAMA_PID=$!
        for i in {1..20}; do
            sleep 1
            if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                ok "Ollama started (PID $OLLAMA_PID)"
                break
            fi
            [[ $i -eq 20 ]] && warn "Ollama didn't respond after 20s — check /tmp/ollama.log"
        done
    fi
fi

# ── 4. Jaeger (optional) ──────────────────────────────────────────────────────
if $JAEGER; then
    if ! command -v docker &>/dev/null; then
        warn "--jaeger requires Docker; skipping"
    elif docker ps --filter "name=jaeger" --format '{{.Names}}' | grep -q jaeger; then
        ok "Jaeger already running  →  http://localhost:16686"
    else
        info "Starting Jaeger..."
        docker run -d --name jaeger \
            -p 16686:16686 \
            -p 4317:4317 \
            jaegertracing/all-in-one:latest \
            &>/dev/null
        ok "Jaeger started  →  http://localhost:16686"
        export OTEL_ENABLED=true
        export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
        info "OTel tracing enabled (OTLP → Jaeger)"
    fi
fi

# ── 5. web server ─────────────────────────────────────────────────────────────
if lsof -ti tcp:"$PORT" &>/dev/null; then
    ok "web-server already on :$PORT  →  http://localhost:$PORT"
else
    info "Starting web server on :$PORT ..."
    # Load .env for the server process
    set -a; source .env; set +a
    WEB_PORT="$PORT" web-server &>/tmp/submissions-web.log 2>&1 &
    WEB_PID=$!

    for i in {1..15}; do
        sleep 1
        if curl -sf "http://localhost:$PORT/api/works" &>/dev/null; then
            ok "Web server ready (PID $WEB_PID)  →  http://localhost:$PORT"
            break
        fi
        [[ $i -eq 15 ]] && {
            warn "Web server didn't respond — check /tmp/submissions-web.log"
            tail -10 /tmp/submissions-web.log
        }
    done
fi

# ── 6. open browser (local only) ─────────────────────────────────────────────
if $OPEN_BROWSER && command -v open &>/dev/null; then
    open "http://localhost:$PORT"
fi

# ── 7. summary ────────────────────────────────────────────────────────────────
echo ""
echo "==========================="
echo "  Web UI   →  http://localhost:$PORT"
$JAEGER && echo "  Jaeger   →  http://localhost:16686" || true
echo ""
echo "Logs:"
echo "  web server:  tail -f /tmp/submissions-web.log"
[[ -n "$OLLAMA_BIN" ]] && echo "  ollama:      tail -f /tmp/ollama.log" || true
echo ""
echo "Stop:"
echo "  kill \$(lsof -ti tcp:$PORT)   # web server"
echo "  pkill ollama                  # ollama"
$JAEGER && echo "  docker stop jaeger            # jaeger" || true
echo ""
