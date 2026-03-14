# Mini Deploy

一个轻量级的 Docker 部署管理服务，提供 Web UI 和 REST API，支持 Python、Java、前端等多种项目类型的自动化 CI/CD 流水线。

> A lightweight Docker deployment manager with Web UI and REST API. Supports Python, Java, frontend, and generic Docker projects with automated CI/CD pipelines.

---

## ✨ 功能特性

- **多项目类型支持** — Python（智能依赖缓存）、Java（Maven/Gradle）、前端（Node.js）、通用 Docker
- **实时日志流** — Server-Sent Events 推送，部署过程零延迟可见
- **自动回滚** — 健康检查失败时自动回滚到上一个镜像版本
- **钩子系统** — 支持 `pre_build`、`post_build`、`pre_deploy`、`post_deploy` 四个阶段的自定义脚本
- **私有仓库** — 支持 GitHub Token 认证
- **Web UI** — 无需命令行，浏览器即可管理所有项目部署

---

## 快速安装

> 前提条件：已安装 **Docker** 和 **Docker Compose**

一行命令完成安装：

```bash
curl -fsSL https://raw.githubusercontent.com/byronicefield/mini-deploy/main/install.sh | bash
```

脚本会自动完成：克隆源码 → 本地构建 Docker 镜像 → 生成 `docker-compose.yml` 和 `.env` → 启动服务。

安装完成后访问：**http://localhost:9999**

---

## 手动安装

### 方式一：Docker Compose（推荐）

**1. 生成配置文件（不启动）**

```bash
curl -fsSL https://raw.githubusercontent.com/byronicefield/mini-deploy/main/install.sh | bash -s -- --no-start
```

**2. 编辑配置**

```bash
vim .env
```

**3. 启动服务**

```bash
docker compose up -d
```

---

### 方式二：从源码运行

```bash
git clone https://github.com/byronicefield/mini-deploy.git
cd mini-deploy

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 填写配置

uvicorn main:app --host 0.0.0.0 --port 9999 --reload
```

---

## 配置说明

环境变量（`.env` 文件）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_PATH` | `/logs` | 日志存储目录 |
| `DEPLOY_GITHUB_TOKEN` | 空 | GitHub Personal Access Token（私有仓库必填） |
| `DEPLOY_COMPOSE_FILE` | `/pi-cluster/docker-compose.yml` | 默认 docker-compose 文件路径 |

---

## 项目接入规范

### Python 项目

- 仓库根目录或 `deploy/` 目录需含 `Dockerfile`
- 推荐使用 multi-stage 构建（自动检测 `requirements.txt` 变化，复用 base 层缓存）

```dockerfile
FROM python:3.9-slim AS base
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM base AS app
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Java 项目

- 需含 `pom.xml`（Maven）或 `build.gradle`（Gradle）
- 需含 `Dockerfile` 用于打包 JAR

### 前端项目

- 需含 `package.json`
- 配置项：`build_command`（默认 `npm run build`）、`dist_dir`（默认 `dist`）、`deploy_target_dir`

### 通用 Docker 项目

- 仓库含 `Dockerfile` 即可，直接执行 `docker build`

---

## 钩子脚本

在项目仓库的 `deploy/hooks/` 目录下放置以下脚本，部署时自动执行：

| 文件 | 触发时机 |
|------|----------|
| `pre_build.sh` | 构建镜像前 |
| `post_build.sh` | 构建镜像后 |
| `pre_deploy.sh` | `docker compose up` 前 |
| `post_deploy.sh` | 部署完成后 |

脚本可使用以下环境变量：

```bash
IMAGE          # 当前镜像名
ROLLBACK_TAG   # 回滚镜像标签
SERVICE        # 服务名
COMPOSE_FILE   # docker-compose 文件路径
REPO_DIR       # 仓库临时目录
PROJECT_ID     # 工程 ID
```

---

## API 文档

| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/` | Web 管理界面 |
| `POST` | `/projects` | 新增工程 |
| `GET` | `/projects` | 获取所有工程及状态 |
| `PUT` | `/projects/{id}` | 更新工程配置 |
| `DELETE` | `/projects/{id}` | 删除工程 |
| `POST` | `/projects/{id}/trigger` | 触发部署 |
| `POST` | `/projects/{id}/rollback` | 手动回滚 |
| `GET` | `/projects/{id}/status` | 查询部署状态 |
| `GET` | `/projects/{id}/logs` | 获取完整日志 |
| `GET` | `/projects/{id}/stream` | SSE 实时日志流 |
| `POST` | `/config/update` | 更新全局配置 |

---

## 部署流程说明

```
触发部署
  │
  ├─ 1. Pulling   — git clone --depth=1 拉取最新代码
  ├─ 2. Building  — 根据项目类型选择构建策略
  │     ├─ Python:   检测 requirements.txt → 复用/重建 base 镜像
  │     ├─ Java:     Maven/Gradle 编译 → 打包 JAR → docker build
  │     ├─ Frontend: Node 容器 npm build → 复制产物
  │     └─ Generic:  docker build
  ├─ 3. Deploying — docker compose up -d --no-deps {service}
  ├─ 4. Checking  — 健康检查（20s 超时）
  └─ 5. Done / Rollback（失败自动回滚）
```

---

## License

MIT
