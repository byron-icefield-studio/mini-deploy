# Stage 1: base deps（依赖层，pyproject.toml + uv.lock 不变时复用缓存）
# Base stage: uv official image + system deps. Only rebuilt when pyproject.toml or uv.lock changes.
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS base

# 安装系统依赖和 Docker CLI / Install system deps and Docker CLI
RUN apt-get update && \
    apt-get install -y git apt-transport-https ca-certificates curl gnupg lsb-release && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli

# 仅复制依赖描述文件，充分利用层缓存
# Copy only dependency manifests to maximize layer cache reuse
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 2: app（业务层，每次部署重新 COPY 代码）
# App stage: copy source code on top of base. Fast rebuild since uv cache is reused.
FROM base AS app

EXPOSE 80
WORKDIR /app
COPY . .
# 安装项目本身（跳过依赖，已在 base 层安装）
# Install the project itself (deps already installed in base stage)
RUN uv sync --frozen --no-dev
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
