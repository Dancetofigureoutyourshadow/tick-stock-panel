#!/usr/bin/env bash
# tickflow-stock-panel — 构建前端并以单进程启动生产服务
#
# 用法:
#   ./prod.sh                   # 默认监听 0.0.0.0:3018
#   BACKEND_PORT=8000 ./prod.sh
#
# 生产模式由 FastAPI 同时提供 API 和 frontend/dist，不启动 Vite。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"

# 只读取启动器需要的配置，避免把 .env 当作 shell 脚本执行。
read_dotenv_value() {
  local key="$1"
  if [[ ! -f "$ROOT/.env" ]]; then
    return 0
  fi
  awk -v wanted="$key" '
    $0 ~ "^[[:space:]]*" wanted "[[:space:]]*=" {
      sub(/^[^=]*=/, "")
      sub(/[[:space:]]+#.*$/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      if (($0 ~ /^".*"$/) || ($0 ~ /^\047.*\047$/)) {
        $0 = substr($0, 2, length($0) - 2)
      }
      print
      exit
    }
  ' "$ROOT/.env"
}

ENV_HOST="$(read_dotenv_value HOST)"
ENV_PORT="$(read_dotenv_value PORT)"
BACKEND_HOST="${HOST:-${ENV_HOST:-0.0.0.0}}"
# 保持与 dev.sh 一致：BACKEND_PORT 优先，其次兼容 PORT。
BACKEND_PORT="${BACKEND_PORT:-${PORT:-${ENV_PORT:-3018}}}"

UVICORN_ENV_ARGS=()
if [[ -f "$ROOT/.env" ]]; then
  UVICORN_ENV_ARGS=(--env-file "$ROOT/.env")
fi

DISPLAY_HOST="$BACKEND_HOST"
if [[ "$DISPLAY_HOST" == "0.0.0.0" || "$DISPLAY_HOST" == "::" ]]; then
  DISPLAY_HOST="localhost"
fi

# 与 dev.sh / Docker 保持一致，支持 BACKEND_EXTRAS=legacy-cpu backtest。
if [[ -z "${BACKEND_EXTRAS+x}" && -f "$ROOT/.env" ]]; then
  BACKEND_EXTRAS="$(read_dotenv_value BACKEND_EXTRAS)"
fi
BACKEND_EXTRAS="${BACKEND_EXTRAS:-}"
BACKEND_EXTRA_ARGS=()
if [[ -n "$BACKEND_EXTRAS" ]]; then
  read -r -a backend_extras <<< "$BACKEND_EXTRAS"
  for extra in "${backend_extras[@]}"; do
    BACKEND_EXTRA_ARGS+=(--extra "$extra")
  done
fi

BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
GRAY='\033[0;90m'
NC='\033[0m'

info() { echo -e "${GRAY}[prod]${NC} $*"; }
ok()   { echo -e "${GREEN}[prod]${NC} $*"; }
err()  { echo -e "${RED}[prod]${NC} $*" >&2; }

require_cmd() {
  local cmd="$1" hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    err "$cmd 未安装"
    echo "       安装方式: $hint"
    exit 1
  fi
}

require_cmd uv   "curl -LsSf https://astral.sh/uv/install.sh | sh"
require_cmd pnpm "npm i -g pnpm 或 corepack enable"

# Git Bash / MSYS 通常没有 lsof，优先使用 Windows 自带 netstat。
WINDOWS_BASH=0
if command -v netstat.exe >/dev/null 2>&1; then
  WINDOWS_BASH=1
fi

port_pids() {
  local port="$1"
  if [ "$WINDOWS_BASH" -eq 1 ]; then
    netstat.exe -ano -p tcp 2>/dev/null \
      | tr -d '\r' \
      | awk -v suffix=":$port" '
          $1 == "TCP" && $2 ~ (suffix "$") && $4 == "LISTENING" && !seen[$5]++ { print $5 }
        '
    return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    return 0
  fi
  err "无法检查端口 $port：需要 lsof 或 Windows 的 netstat.exe"
  return 1
}

require_port_free() {
  local pids
  pids="$(port_pids "$BACKEND_PORT")" || exit 1
  if [ -n "$pids" ]; then
    err "端口 $BACKEND_PORT 已被占用，生产启动器不会结束外部进程。PID: $(echo "$pids" | xargs)"
    exit 1
  fi
}

echo
echo -e "${BLUE}tickflow-stock-panel production${NC}"
echo -e "  backend: ${YELLOW}http://$DISPLAY_HOST:$BACKEND_PORT${NC}"
echo -e "  frontend: ${YELLOW}由 FastAPI 提供${NC}"
echo

# 首次运行才安装依赖；已有环境不做同步，避免移除独立安装的 MooTDX 等插件包。
if [ ! -d "$BACKEND_DIR/.venv" ] || [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
  if [ "${#BACKEND_EXTRA_ARGS[@]}" -gt 0 ]; then
    info "同步后端 Python 依赖，extras: $BACKEND_EXTRAS"
  else
    info "首次启动，安装后端 Python 依赖..."
  fi
  ( cd "$BACKEND_DIR" && uv sync --frozen --inexact ${BACKEND_EXTRA_ARGS[@]+"${BACKEND_EXTRA_ARGS[@]}"} )
  ok "后端依赖已就绪"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  info "首次启动，安装前端 Node 依赖..."
  ( cd "$FRONTEND_DIR" && pnpm install )
  ok "前端依赖已就绪"
fi

info "构建前端静态资源..."
( cd "$FRONTEND_DIR" && pnpm build )
if [ ! -f "$FRONTEND_DIR/dist/index.html" ]; then
  err "前端构建完成但未找到 frontend/dist/index.html"
  exit 1
fi
ok "前端构建完成"

# 构建期间端口可能发生变化，因此在真正启动前再次检查。
require_port_free
ok "启动单进程 FastAPI/Uvicorn（无 reload、无多 worker）"

cd "$BACKEND_DIR"
if [ "${#UVICORN_ENV_ARGS[@]}" -gt 0 ]; then
  exec uv run --no-sync python -m uvicorn app.main:app \
    "${UVICORN_ENV_ARGS[@]}" --host "$BACKEND_HOST" --port "$BACKEND_PORT"
fi
exec uv run --no-sync python -m uvicorn app.main:app \
  --host "$BACKEND_HOST" --port "$BACKEND_PORT"
