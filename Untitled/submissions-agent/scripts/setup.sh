#!/usr/bin/env bash
# One-time setup for submissions-agent.
# Run once from the project root after cloning.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ── colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }
step() { echo -e "\n${GREEN}▶${NC} $*"; }

echo "submissions-agent — first-time setup"
echo "======================================"

# ── 1. Python ────────────────────────────────────────────────────────────────
step "Python / pyenv"

# Prefer the version pyenv manages; fall back to system python3
if command -v pyenv &>/dev/null; then
    eval "$(pyenv init -)" 2>/dev/null || true
fi

PYTHON=$(command -v python3.13 || command -v python3 || echo "")
[[ -z "$PYTHON" ]] && die "python3 not found — install via pyenv: pyenv install 3.13.0"

PY_VER=$("$PYTHON" --version 2>&1)
ok "Using $PYTHON  ($PY_VER)"

# Pin the version for this project so future shells auto-select it
"$PYTHON" -c "import sys; assert sys.version_info >= (3,11), 'need Python 3.11+'" \
    || die "Python 3.11+ required"

# ── 2. Install package ───────────────────────────────────────────────────────
step "pip install -e ."
"$PYTHON" -m pip install -e . --quiet
ok "Package installed (CLI commands available)"

# ── 3. Playwright ────────────────────────────────────────────────────────────
step "Playwright chromium"
if "$PYTHON" -m playwright install chromium --quiet 2>/dev/null; then
    ok "Chromium installed"
else
    warn "Playwright install failed — Chill Subs scraping will not work (non-fatal)"
fi

# ── 4. .env ──────────────────────────────────────────────────────────────────
step ".env file"
if [[ -f .env ]]; then
    ok ".env already exists — skipping copy"
else
    cp .env.example .env
    ok "Created .env from .env.example"
    warn "Open .env and fill in: ANTHROPIC_API_KEY, BW_CLIENTID, BW_CLIENTSECRET"
    warn "TWEEK_CALENDAR_ID can be found after first run — see README"
fi

# ── 5. data/ directory ───────────────────────────────────────────────────────
step "data/ directory"
mkdir -p data/chroma data/manuscripts
touch data/events.jsonl 2>/dev/null || true
ok "data/ ready"

# ── 6. Ollama models ─────────────────────────────────────────────────────────
step "Ollama models"

OLLAMA_BIN=$(command -v ollama \
    || ls /Users/"$USER"/Applications/Ollama.app/Contents/Resources/ollama 2>/dev/null \
    || ls /Applications/Ollama.app/Contents/Resources/ollama 2>/dev/null \
    || echo "")

if [[ -z "$OLLAMA_BIN" ]]; then
    warn "ollama not found — install from https://ollama.com then re-run this script"
    warn "Required models: qwen2.5 (agent), nomic-embed-text (RAG)"
else
    ok "ollama found at $OLLAMA_BIN"

    # Start ollama serve in background if not already running
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        "$OLLAMA_BIN" serve &>/dev/null &
        OLLAMA_PID=$!
        echo "  waiting for ollama to start..."
        for i in {1..15}; do
            sleep 1
            curl -sf http://localhost:11434/api/tags &>/dev/null && break
            [[ $i -eq 15 ]] && warn "ollama didn't respond — skipping model pull"
        done
    fi

    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        INSTALLED=$("$OLLAMA_BIN" list 2>/dev/null | awk 'NR>1{print $1}')

        if echo "$INSTALLED" | grep -q "qwen2.5"; then
            ok "qwen2.5 already installed"
        else
            echo "  pulling qwen2.5 (~4.7 GB)..."
            "$OLLAMA_BIN" pull qwen2.5
            ok "qwen2.5 installed"
        fi

        if echo "$INSTALLED" | grep -q "nomic-embed-text"; then
            ok "nomic-embed-text already installed"
        else
            echo "  pulling nomic-embed-text (~274 MB)..."
            "$OLLAMA_BIN" pull nomic-embed-text
            ok "nomic-embed-text installed (RAG embeddings)"
        fi
    fi
fi

# ── 7. Summary ───────────────────────────────────────────────────────────────
echo ""
echo "======================================"
ok "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Fill in .env  (ANTHROPIC_API_KEY, BW_CLIENTID, BW_CLIENTSECRET)"
echo "  2. Run: ./scripts/start.sh"
echo ""
