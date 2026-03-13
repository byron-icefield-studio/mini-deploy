# Stage 1: base deps（依赖层，requirements.txt 不变时复用缓存）
# Base stage: system deps + pip packages. Only rebuilt when requirements.txt changes.
FROM python:3.9-slim AS base

RUN apt-get update && \
    apt-get install -y git apt-transport-https ca-certificates curl gnupg lsb-release && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli && \
    pip install --upgrade pip

COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt

# Stage 2: app（业务层，每次部署重新 COPY 代码）
# App stage: copy source code on top of base. Fast rebuild since pip is cached.
FROM base AS app

EXPOSE 80
WORKDIR /app
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
