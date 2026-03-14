#!/usr/bin/env bash
# install.sh — Mini Deploy 一键安装脚本
# One-liner: curl -fsSL https://raw.githubusercontent.com/byronicefield/mini-deploy/main/install.sh | bash
#
# 功能 / Features:
#   1. 检查前置依赖（Docker、Docker Compose）
#   2. 克隆源码并在本地构建 Docker 镜像
#   3. 在当前目录生成 docker-compose.yml 和 .env
#   4. 启动服务（除非传入 --no-start）

REPO_URL="https://github.com/byronicefield/mini-deploy.git"
IMAGE_NAME="mini-deploy:latest"

set -euo pipefail

# ─── 颜色输出 / Colored output ───────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[mini-deploy]${NC} $*"; }
success() { echo -e "${GREEN}[mini-deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[mini-deploy]${NC} $*"; }
error()   { echo -e "${RED}[mini-deploy]${NC} $*" >&2; exit 1; }

# ─── 参数解析 / Argument parsing ─────────────────────────────────────────────
AUTO_START=true
for arg in "$@"; do
  case "$arg" in
    --no-start) AUTO_START=false ;;
  esac
done

# ─── Banner ──────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "  __  __ _       _   ____             _             "
echo " |  \/  (_)_ __ (_) |  _ \  ___ _ __ | | ___  _   _ "
echo " | |\/| | | '_ \| | | | | |/ _ \ '_ \| |/ _ \| | | |"
echo " | |  | | | | | | | | |_| |  __/ |_) | | (_) | |_| |"
echo " |_|  |_|_|_| |_|_| |____/ \___| .__/|_|\___/ \__, |"
echo "                                |_|             |___/ "
echo -e "${NC}"
info "轻量级 Docker 部署管理服务 / Lightweight Docker Deployment Manager"
echo ""

# ─── 检查依赖 / Check prerequisites ──────────────────────────────────────────
info "检查前置依赖 / Checking prerequisites..."

if ! command -v docker &>/dev/null; then
  error "未找到 Docker，请先安装：https://docs.docker.com/get-docker/"
fi

# 支持 docker compose (v2) 和 docker-compose (v1)
# Support both docker compose (v2) and docker-compose (v1)
if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
else
  error "未找到 Docker Compose，请先安装：https://docs.docker.com/compose/install/"
fi

success "Docker 已就绪: $(docker --version)"
success "Docker Compose 已就绪: $($COMPOSE_CMD version)"
echo ""

# ─── 安装目录 / Install directory ────────────────────────────────────────────
INSTALL_DIR="${MINI_DEPLOY_DIR:-$(pwd)/mini-deploy}"

# 如果通过 pipe 运行，直接在当前目录安装
# If piped from curl, install to current directory
if [ ! -t 0 ]; then
  INSTALL_DIR="$(pwd)/mini-deploy"
fi

mkdir -p "$INSTALL_DIR"

info "安装目录 / Install directory: ${BOLD}$INSTALL_DIR${NC}"
echo ""

# ─── 克隆源码并构建镜像 / Clone source and build image ───────────────────────
SRC_DIR="$INSTALL_DIR/src"

if [ -d "$SRC_DIR/.git" ]; then
  info "更新源码 / Updating source..."
  git -C "$SRC_DIR" pull --ff-only
else
  info "克隆源码 / Cloning source from $REPO_URL ..."
  git clone --depth=1 "$REPO_URL" "$SRC_DIR"
fi

info "构建 Docker 镜像 / Building Docker image: ${IMAGE_NAME} ..."
docker build -f "$SRC_DIR/deploy/Dockerfile" -t "$IMAGE_NAME" "$SRC_DIR"
success "镜像构建完成 / Image built: ${IMAGE_NAME}"
echo ""

cd "$INSTALL_DIR"

# ─── 配置收集 / Configuration prompts ────────────────────────────────────────
# 交互模式下提示用户输入；非交互时（curl | bash）使用默认值
# Interactive: prompt user; Non-interactive (curl | bash): use defaults

PORT=9999
GITHUB_TOKEN=""
COMPOSE_FILE="/pi-cluster/docker-compose.yml"
LOG_DIR="$INSTALL_DIR/logs"
DATA_DIR="$INSTALL_DIR/data"

if [ -t 0 ]; then
  echo -e "${BOLD}配置 Mini Deploy / Configure Mini Deploy${NC}"
  echo ""

  read -rp "  服务端口 / Port [9999]: " input_port
  PORT="${input_port:-9999}"

  read -rp "  GitHub Token（私有仓库必填，可留空）/ GitHub Token [empty]: " input_token
  GITHUB_TOKEN="${input_token:-}"

  read -rp "  默认 docker-compose.yml 路径 / Default compose file [/pi-cluster/docker-compose.yml]: " input_compose
  COMPOSE_FILE="${input_compose:-/pi-cluster/docker-compose.yml}"

  read -rp "  日志目录 / Log directory [$LOG_DIR]: " input_log
  LOG_DIR="${input_log:-$LOG_DIR}"

  echo ""
fi

mkdir -p "$LOG_DIR" "$DATA_DIR"

# ─── 生成 .env / Generate .env ───────────────────────────────────────────────
if [ -f ".env" ]; then
  warn ".env 已存在，跳过生成（如需重置请手动删除）/ .env already exists, skipping"
else
  cat > .env <<EOF
# Mini Deploy 配置 / Configuration
# 生成时间 / Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

# 日志目录（容器内路径）/ Log directory inside container
LOG_PATH=/logs

# GitHub Personal Access Token（私有仓库必填）/ Required for private repos
DEPLOY_GITHUB_TOKEN=${GITHUB_TOKEN}

# 默认 docker-compose.yml 文件路径（宿主机绝对路径）/ Host path to your docker-compose.yml
DEPLOY_COMPOSE_FILE=${COMPOSE_FILE}
EOF
  success "已生成 .env"
fi

# ─── 生成 docker-compose.yml / Generate docker-compose.yml ──────────────────
if [ -f "docker-compose.yml" ]; then
  warn "docker-compose.yml 已存在，跳过生成 / docker-compose.yml already exists, skipping"
else
  # 挂载宿主机 docker-compose 文件所在目录（如目录不存在则不挂载）
  # Mount the host directory containing your docker-compose file
  COMPOSE_DIR="$(dirname "$COMPOSE_FILE")"
  COMPOSE_MOUNT=""
  if [ "$COMPOSE_DIR" != "/" ] && [ -n "$COMPOSE_DIR" ]; then
    COMPOSE_MOUNT="      - ${COMPOSE_DIR}:${COMPOSE_DIR}"
  fi

  cat > docker-compose.yml <<EOF
# Mini Deploy — docker-compose.yml
# 由 install.sh 自动生成 / Auto-generated by install.sh

services:
  mini-deploy:
    image: mini-deploy:latest
    container_name: mini-deploy
    restart: unless-stopped
    ports:
      - "${PORT}:80"
    volumes:
      # Docker socket（必须挂载，用于构建和部署其他容器）
      # Docker socket — required to build & manage other containers
      - /var/run/docker.sock:/var/run/docker.sock

      # 日志目录 / Log directory
      - ${LOG_DIR}:/logs

      # 配置目录（持久化 .env 配置）/ Config directory
      - ${INSTALL_DIR}:/config

      # 你的 docker-compose.yml 所在目录（按需修改）
      # Directory containing your docker-compose.yml (adjust as needed)
$([ -n "$COMPOSE_MOUNT" ] && echo "$COMPOSE_MOUNT" || echo "      # - /your/compose/dir:/your/compose/dir")
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/projects"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
EOF
  success "已生成 docker-compose.yml"
fi

# ─── 启动服务 / Start service ─────────────────────────────────────────────────
if [ "$AUTO_START" = true ]; then
  echo ""
  info "启动服务 / Starting service..."
  $COMPOSE_CMD up -d

  echo ""
  success "Mini Deploy 已启动！/ Mini Deploy is running!"
  echo ""
  echo -e "  ${BOLD}访问地址 / Web UI:${NC}  http://localhost:${PORT}"
  echo -e "  ${BOLD}日志目录 / Logs:${NC}    $LOG_DIR"
  echo -e "  ${BOLD}安装目录 / Dir:${NC}     $INSTALL_DIR"
  echo ""
  echo -e "  管理命令 / Management:"
  echo -e "    cd $INSTALL_DIR"
  echo -e "    $COMPOSE_CMD logs -f       # 查看日志"
  echo -e "    $COMPOSE_CMD down          # 停止服务"
  echo -e "    # 更新版本 / Update:"
  echo -e "    git -C $INSTALL_DIR/src pull && docker build -f $INSTALL_DIR/src/deploy/Dockerfile -t $IMAGE_NAME $INSTALL_DIR/src && $COMPOSE_CMD up -d"
  echo ""
else
  echo ""
  success "配置文件已生成 / Config files generated"
  echo ""
  echo -e "  启动服务 / Start service:"
  echo -e "    cd $INSTALL_DIR && $COMPOSE_CMD up -d"
  echo ""
fi
