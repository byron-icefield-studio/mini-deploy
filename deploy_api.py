"""部署管理 API 与 Web UI。
Deploy management API and Web UI.

支持的工程类型 / Supported project types:
- 通用 (generic): 标准 Dockerfile 构建
- Python: 自动 base stage 缓存（requirements.txt hash 检测）
- Java: Maven/Gradle 构建 → Docker 镜像
- 前端 (frontend): npm build → 静态文件部署到 nginx
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

import state
from config import settings

# Jinja2 模板引擎 / Jinja2 template engine
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,
)

deploy_router = APIRouter()

# 项目接入规范 HTML，从独立文件加载（避免在代码中维护大段 HTML）
# Load onboarding spec HTML from a separate file to keep this module clean
_SPEC_HTML = (Path(__file__).parent / "deploy_spec.html").read_text(encoding="utf-8")

# ── Project persistence / 工程持久化 ─────────────────────────────────────────


def _projects_file() -> Path:
    """工程配置文件路径。
    Path to the projects JSON config file.
    """
    from config import ENV_FILE
    return ENV_FILE.parent / "deploy_projects.json"


def load_deploy_projects() -> List[dict]:
    """加载所有工程配置。
    Load all project configurations.
    """
    f = _projects_file()
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_projects(projects: List[dict]):
    """保存工程配置到 JSON 文件。
    Save project configurations to JSON file.
    """
    _projects_file().write_text(
        json.dumps(projects, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _find_project(project_id: str) -> Optional[dict]:
    """按 ID 查找工程。
    Find project by ID.
    """
    for p in load_deploy_projects():
        if p["id"] == project_id:
            return p
    return None


# ── Log file helpers / 日志文件工具 ──────────────────────────────────────────


def _log_file(project_id: str) -> Path:
    """部署日志文件路径。
    Path to deploy log file for a project.
    """
    return Path(settings.LOG_PATH) / f"deploy_{project_id}.log"


def _cleanup_deploy_logs(max_age_hours: int = 24):
    """清理过期部署日志。
    Clean up deploy logs older than max_age_hours.
    """
    import time
    cutoff = time.time() - max_age_hours * 3600
    for f in Path(settings.LOG_PATH).glob("deploy_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


# ── Per-project SSE & tasks / 工程级 SSE 和异步任务 ──────────────────────────

_subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
_tasks: Dict[str, asyncio.Task] = {}


def _broadcast(project_id: str, line: str):
    """广播日志行到 SSE 订阅者和日志文件。
    Broadcast log line to SSE subscribers and log file.
    """
    state.deploy.get_project(project_id).logs.append(line)
    try:
        with open(_log_file(project_id), "a", encoding="utf-8") as _f:
            _f.write(line + "\n")
    except Exception:
        pass
    for q in _subscribers[project_id]:
        q.put_nowait(line)


def _broadcast_step(project_id: str, step: str):
    """更新当前流程步骤并通过 SSE 广播步骤事件（进度条用，不写入日志）。
    Update current pipeline step and push __step:xxx__ event via SSE (for progress bar, not logged).
    """
    state.deploy.get_project(project_id).step = step
    for q in _subscribers[project_id]:
        q.put_nowait(f"__step:{step}__")


async def _run_cmd(project_id: str, *args, extra_env: dict = None) -> int:
    """执行外部命令，实时广播输出到日志流。
    Run external command, broadcasting stdout/stderr to the log stream in real time.
    """
    run_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "GIT_FLUSH": "1",
        "BUILDKIT_PROGRESS": "plain",
    }
    if extra_env:
        run_env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # 不能按"行"读取：git/docker 常用 '\r' 更新进度条，按行会卡到很晚才吐日志
    # Can't read by lines: git/docker uses '\r' for progress, line-buffering delays output
    buf = ""
    while True:
        raw = await proc.stdout.read(1024)
        if not raw:
            break
        buf += raw.decode(errors="replace").replace("\r", "\n")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if line:
                _broadcast(project_id, line)

    tail = buf.strip()
    if tail:
        _broadcast(project_id, tail)

    await proc.wait()
    return proc.returncode


async def _run_hook(project_id: str, hook_name: str, tmp_dir: str, hook_env: dict):
    """执行钩子脚本（若存在）。不存在则静默跳过，失败则抛出异常。
    Run hook script deploy/hooks/{hook_name}.sh if it exists.
    """
    hook_script = os.path.join(tmp_dir, "deploy", "hooks", f"{hook_name}.sh")
    if not os.path.isfile(hook_script):
        return
    _broadcast(project_id, f"==> [{hook_name}] 执行钩子...")
    rc = await _run_cmd(project_id, "bash", hook_script, extra_env=hook_env)
    if rc != 0:
        raise RuntimeError(f"{hook_name} hook 执行失败 (exit {rc})")


async def _get_image_label(image: str, label: str) -> str:
    """读取 docker image label 值，镜像不存在时返回空字符串。
    Read a label value from a docker image; returns '' if image not found.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format", f'{{{{index .Config.Labels "{label}"}}}}',
            image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


async def _get_docker_status(service_name: str) -> str:
    """获取 docker compose 服务的运行状态。
    Get docker compose service running status.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a",
            "--filter", f"label=com.docker.compose.service={service_name}",
            "--format", "{{.Status}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        lines = [l for l in stdout.decode().strip().split("\n") if l]
        return lines[0] if lines else "—"
    except Exception:
        return "—"


# ── Java build helpers / Java 构建工具 ───────────────────────────────────────


async def _build_java(project_id: str, tmp_dir: str, project: dict) -> None:
    """在临时容器内执行 Java 构建（Maven 或 Gradle）。
    Build Java project inside a temporary container (Maven or Gradle).

    自动检测构建工具：
    - 存在 pom.xml → Maven (mvn package -DskipTests)
    - 存在 build.gradle / build.gradle.kts → Gradle (gradle build -x test)
    - 支持自定义构建命令覆盖
    """
    java_version = project.get("java_version") or "17"
    build_cmd = project.get("build_command") or ""

    # 自动检测构建工具 / Auto-detect build tool
    has_pom = os.path.isfile(os.path.join(tmp_dir, "pom.xml"))
    has_gradle = (
        os.path.isfile(os.path.join(tmp_dir, "build.gradle")) or
        os.path.isfile(os.path.join(tmp_dir, "build.gradle.kts"))
    )
    has_gradlew = os.path.isfile(os.path.join(tmp_dir, "gradlew"))

    if not build_cmd:
        if has_pom:
            build_cmd = "mvn package -DskipTests -B"
        elif has_gradle:
            if has_gradlew:
                build_cmd = "chmod +x gradlew && ./gradlew build -x test"
            else:
                build_cmd = "gradle build -x test"
        else:
            raise RuntimeError("未找到 pom.xml 或 build.gradle，无法确定 Java 构建方式")

    # 选择构建镜像 / Choose build image
    if has_pom:
        build_image = f"maven:{java_version}-eclipse-temurin"
        _broadcast(project_id, f"==> [Java] 检测到 Maven 项目（Java {java_version}）")
    else:
        build_image = f"gradle:{java_version}-jdk"
        _broadcast(project_id, f"==> [Java] 检测到 Gradle 项目（Java {java_version}）")

    ctr_name = f"java-build-{uuid.uuid4().hex[:8]}"
    _broadcast(project_id, f"==> [2/3] Java build（镜像: {build_image}，命令: {build_cmd}）")

    try:
        # 创建构建容器 / Create build container
        rc = await _run_cmd(
            project_id,
            "docker", "create", "--name", ctr_name, "-w", "/app",
            build_image,
            "sh", "-c", build_cmd,
        )
        if rc != 0:
            raise RuntimeError("创建 Java 构建容器失败")

        # 复制源码到容器 / Copy source into container
        rc = await _run_cmd(project_id, "docker", "cp", f"{tmp_dir}/.", f"{ctr_name}:/app")
        if rc != 0:
            raise RuntimeError("复制源码到构建容器失败")

        # 启动构建 / Start build
        rc = await _run_cmd(project_id, "docker", "start", "-a", ctr_name)
        if rc != 0:
            raise RuntimeError(f"Java build 失败 (exit {rc})")

        # 将构建产物复制回宿主机 / Copy build output back to host
        # Maven: target/, Gradle: build/libs/
        if has_pom:
            rc = await _run_cmd(project_id, "docker", "cp", f"{ctr_name}:/app/target", f"{tmp_dir}/target")
        else:
            rc = await _run_cmd(project_id, "docker", "cp", f"{ctr_name}:/app/build", f"{tmp_dir}/build")
        if rc != 0:
            _broadcast(project_id, "    （复制构建产物时出现警告，继续执行）")

    finally:
        _p = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", ctr_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _p.wait()


# ── Deploy pipeline / 部署流水线 ─────────────────────────────────────────────


async def _deploy_pipeline(project_id: str, project: dict):
    """完整部署流水线：clone → build → deploy。
    Full deploy pipeline: clone → build → deploy.
    """
    ps = state.deploy.get_project(project_id)
    started = datetime.now()
    ps.started_at = started
    ps.finished_at = None
    ps.logs = []
    ps.error = None
    ps.step = "idle"
    tmp_dir = None
    _cleanup_deploy_logs()
    try:
        _log_file(project_id).write_text("", encoding="utf-8")
        repo_url = project["repo_url"]
        token = project.get("github_token") or settings.DEPLOY_GITHUB_TOKEN
        if token and repo_url.startswith("https://"):
            repo_url = repo_url.replace("https://", f"https://{token}@", 1)

        # Step 1: git clone
        ps.phase = "pulling"
        _broadcast_step(project_id, "clone")
        tmp_dir = tempfile.mkdtemp(prefix="deploy_")
        _broadcast(project_id, f"==> [1/3] git clone {project['repo_url']}")
        rc = await _run_cmd(project_id, "git", "clone", "--depth=1", "--progress", repo_url, tmp_dir)
        if rc != 0:
            raise RuntimeError(f"git clone failed (exit {rc})")

        project_type = project.get("project_type") or ""
        is_frontend = project_type == "frontend"
        is_java = project_type == "java"

        # Step 1.5: 构建前保存 rollback 镜像（Docker 类型专用，前端除外）
        # Save rollback image before overwriting :latest (Docker projects only, not frontend)
        image = None
        image_base = None
        rollback_tag = None
        if not is_frontend:
            _broadcast_step(project_id, "backup")
            image = project.get("image_name") or f"{project['service_name']}:latest"
            image_base = image.rsplit(":", 1)[0]
            rollback_tag = f"{image_base}:rollback"
            _broadcast(project_id, f"==> [备份] 保存当前镜像 → {rollback_tag}")
            _check = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", rollback_tag,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _check.wait()
            if _check.returncode == 0:
                await _run_cmd(project_id, "docker", "rmi", rollback_tag)
            rb_rc = await _run_cmd(project_id, "docker", "tag", image, rollback_tag)
            if rb_rc != 0:
                _broadcast(project_id, "    （无旧镜像可备份，首次部署）")

        # 钩子公共环境变量 / Hook env vars
        hook_env = {
            "IMAGE":        image or "",
            "ROLLBACK_TAG": rollback_tag or "",
            "SERVICE":      project.get("service_name") or "",
            "COMPOSE_FILE": project.get("compose_file") or settings.DEPLOY_COMPOSE_FILE or "",
            "REPO_DIR":     tmp_dir,
            "PROJECT_ID":   project_id,
        }

        # Step 2: build
        ps.phase = "building"

        # pre_build 钩子 / pre_build hook
        _broadcast_step(project_id, "pre_build")
        await _run_hook(project_id, "pre_build", tmp_dir, hook_env)

        base_image = None
        if is_frontend:
            # ── 前端构建 / Frontend build ──
            node_ver = project.get("node_version") or "lts"
            build_cmd = project.get("build_command") or "npm run build"
            app_sub = (project.get("app_dir") or "").strip("/")
            work_dir = f"/app/{app_sub}" if app_sub else "/app"
            dist_dir_name_pre = project.get("dist_dir") or "dist"
            dist_in_ctr = f"{work_dir}/{dist_dir_name_pre}"
            dist_on_host = (
                os.path.join(tmp_dir, app_sub, dist_dir_name_pre) if app_sub
                else os.path.join(tmp_dir, dist_dir_name_pre)
            )
            ctr_name = f"fe-build-{uuid.uuid4().hex[:8]}"
            _broadcast_step(project_id, "build")
            _broadcast(project_id, f"==> [2/3] npm build（node:{node_ver}-alpine，workdir:{work_dir}，命令: {build_cmd}）")
            try:
                rc = await _run_cmd(
                    project_id,
                    "docker", "create", "--name", ctr_name, "-w", work_dir,
                    f"node:{node_ver}-alpine",
                    "sh", "-c",
                    f"if [ -f package-lock.json ]; then npm ci; else npm install; fi && {build_cmd}",
                )
                if rc != 0:
                    raise RuntimeError("创建构建容器失败")
                rc = await _run_cmd(project_id, "docker", "cp", f"{tmp_dir}/.", f"{ctr_name}:/app")
                if rc != 0:
                    raise RuntimeError("复制源码到构建容器失败")
                rc = await _run_cmd(project_id, "docker", "start", "-a", ctr_name)
                if rc != 0:
                    raise RuntimeError(f"npm build 失败 (exit {rc})")
                rc = await _run_cmd(project_id, "docker", "cp", f"{ctr_name}:{dist_in_ctr}", dist_on_host)
                if rc != 0:
                    raise RuntimeError("复制构建产物失败")
            finally:
                _p = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", ctr_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await _p.wait()

        elif is_java:
            # ── Java 构建 / Java build ──
            _broadcast_step(project_id, "build")
            await _build_java(project_id, tmp_dir, project)

            # Java 构建完成后，用 Dockerfile 打包最终镜像
            # After Java build, use Dockerfile to package the final image
            deploy_dockerfile = os.path.join(tmp_dir, "deploy", "Dockerfile")
            root_dockerfile = os.path.join(tmp_dir, "Dockerfile")
            if os.path.isfile(deploy_dockerfile):
                dockerfile = deploy_dockerfile
            elif os.path.isfile(root_dockerfile):
                dockerfile = root_dockerfile
            else:
                raise RuntimeError("未找到 Dockerfile（deploy/Dockerfile 或根目录 Dockerfile）。Java 项目需提供 Dockerfile 来打包 JAR")

            _broadcast(project_id, f"==> [2/3] docker build（镜像: {image}，Dockerfile: {os.path.relpath(dockerfile, tmp_dir)}）")
            build_args = ["docker", "build", "--no-cache", "-t", image, "-f", dockerfile, tmp_dir]
            rc = await _run_cmd(project_id, *build_args, extra_env=hook_env)
            if rc != 0:
                raise RuntimeError(f"docker build 失败 (exit {rc})")

        else:
            # ── Docker 构建（通用 / Python）/ Docker build (generic / Python) ──
            deploy_dockerfile = os.path.join(tmp_dir, "deploy", "Dockerfile")
            root_dockerfile = os.path.join(tmp_dir, "Dockerfile")
            if os.path.isfile(deploy_dockerfile):
                dockerfile = deploy_dockerfile
            elif os.path.isfile(root_dockerfile):
                dockerfile = root_dockerfile
            else:
                raise RuntimeError("未找到 Dockerfile（deploy/Dockerfile 或根目录 Dockerfile）")

            # Python 工程：自动检测 requirements.txt 变化，按需重建 base stage
            # Python projects: auto-rebuild base stage when requirements.txt changes
            if project_type == "python":
                base_image = f"{image_base}-base:latest"
                req_file = os.path.join(tmp_dir, "requirements.txt")
                if os.path.isfile(req_file):
                    req_hash = hashlib.sha256(Path(req_file).read_bytes()).hexdigest()
                    last_hash = await _get_image_label(base_image, "req_hash")
                    if req_hash == last_hash:
                        _broadcast(project_id, f"==> [base] requirements.txt 未变化（{req_hash[:8]}），跳过重建")
                    else:
                        _broadcast(project_id, f"==> [base] requirements.txt 已变化（{last_hash[:8] or 'none'} → {req_hash[:8]}），重建 base stage…")
                        rc = await _run_cmd(
                            project_id,
                            "docker", "build",
                            "--target", "base",
                            "--label", f"req_hash={req_hash}",
                            "-t", base_image,
                            "-f", dockerfile,
                            tmp_dir,
                            extra_env=hook_env,
                        )
                        if rc != 0:
                            raise RuntimeError("base stage 构建失败")
                else:
                    _broadcast(project_id, "    （未找到 requirements.txt，跳过 base stage 管理）")

            _broadcast_step(project_id, "build")
            _broadcast(project_id, f"==> [2/3] docker build（镜像: {image}，Dockerfile: {os.path.relpath(dockerfile, tmp_dir)}）")
            build_args = ["docker", "build"]
            if base_image:
                build_args += ["--cache-from", base_image]
            else:
                build_args += ["--no-cache"]
            build_args += ["-t", image, "-f", dockerfile, tmp_dir]
            rc = await _run_cmd(project_id, *build_args, extra_env=hook_env)
            if rc != 0:
                raise RuntimeError(f"docker build 失败 (exit {rc})")

        # post_build 钩子 / post_build hook
        _broadcast_step(project_id, "post_build")
        await _run_hook(project_id, "post_build", tmp_dir, hook_env)

        # Step 3: deploy / 部署
        ps.phase = "restarting"

        # pre_deploy 钩子 / pre_deploy hook
        _broadcast_step(project_id, "pre_deploy")
        await _run_hook(project_id, "pre_deploy", tmp_dir, hook_env)

        _broadcast_step(project_id, "compose_up")
        if is_frontend:
            # 前端部署：复制构建产物到 nginx / Frontend deploy: copy to nginx
            dist_dir_name = project.get("dist_dir") or "dist"
            target_dir = (project.get("deploy_target_dir") or "").rstrip("/")
            nginx_ctr = project.get("nginx_container") or ""
            if not target_dir:
                raise RuntimeError("前端项目需配置「目标路径」")
            src = os.path.join(tmp_dir, app_sub, dist_dir_name) if app_sub else os.path.join(tmp_dir, dist_dir_name)
            if not os.path.isdir(src):
                raise RuntimeError(
                    f"构建产物目录不存在：{dist_dir_name}/（请检查 dist_dir 配置及构建命令）"
                )
            if nginx_ctr:
                _broadcast(project_id, f"==> [3/3] docker cp {dist_dir_name}/ → {nginx_ctr}:{target_dir}/")
                rc = await _run_cmd(
                    project_id, "docker", "exec", nginx_ctr,
                    "sh", "-c", f"rm -rf '{target_dir}'/* 2>/dev/null || true",
                )
                rc = await _run_cmd(
                    project_id, "docker", "cp", f"{src}/.", f"{nginx_ctr}:{target_dir}/",
                )
            else:
                _broadcast(project_id, f"==> [3/3] 部署静态文件 {dist_dir_name}/ → {target_dir}/")
                rc = await _run_cmd(
                    project_id, "bash", "-c",
                    f"set -e && rm -rf '{target_dir}'/* && cp -rp '{src}'/. '{target_dir}/'",
                )
            if rc != 0:
                raise RuntimeError("前端文件部署失败")
            _broadcast(project_id, "==> [部署] 静态文件部署完成 ✓")
            _broadcast_step(project_id, "post_deploy")
            await _run_hook(project_id, "post_deploy", tmp_dir, hook_env)
        else:
            compose_file = project.get("compose_file") or settings.DEPLOY_COMPOSE_FILE
            service = project["service_name"]
            _broadcast(project_id, f"==> [3/3] docker compose up ({service})")
            if project.get("self_service"):
                # 自重启场景 / Self-restart scenario
                ps.phase = "done"
                _pre_persist_result(project_id, started, ps)
                _broadcast(project_id, "==> 结果已持久化，触发重启…")
                try:
                    with open("/etc/hostname") as _f:
                        _self_id = _f.read().strip()
                except Exception:
                    import socket
                    _self_id = socket.gethostname()
                log_file = str(_log_file(project_id))
                health_script = (
                    f"set -e\n"
                    f"COMPOSE_FILE='{compose_file}'\n"
                    f"SERVICE='{service}'\n"
                    f"IMAGE='{image}'\n"
                    f"ROLLBACK='{rollback_tag}'\n"
                    f"LOG='{log_file}'\n"
                    f"\n"
                    f"echo '==> docker compose up...' | tee -a \"$LOG\"\n"
                    f"docker compose -f \"$COMPOSE_FILE\" up -d --no-deps \"$SERVICE\"\n"
                    f"\n"
                    f"echo '==> [健康检查] 等待 25 秒...' >> \"$LOG\"\n"
                    f"sleep 25\n"
                    f"\n"
                    f"if docker ps --filter \"name=$SERVICE\" --filter 'status=running' "
                    f"--format '{{{{.Names}}}}' | grep -q \"$SERVICE\"; then\n"
                    f"    echo '==> [健康检查] 新容器运行正常 ✓' >> \"$LOG\"\n"
                    f"else\n"
                    f"    echo '==> [健康检查] 新容器启动失败，执行回滚...' >> \"$LOG\"\n"
                    f"    docker tag \"$ROLLBACK\" \"$IMAGE\" >> \"$LOG\" 2>&1 || true\n"
                    f"    docker compose -f \"$COMPOSE_FILE\" up -d --no-deps \"$SERVICE\" >> \"$LOG\" 2>&1\n"
                    f"    echo '==> [健康检查] 已回滚到旧版本 !' >> \"$LOG\"\n"
                    f"fi\n"
                )
                await _run_cmd(
                    project_id,
                    "docker", "run", "--rm",
                    "-v", "/var/run/docker.sock:/var/run/docker.sock",
                    "--volumes-from", _self_id,
                    base_image or image,
                    "bash", "-c", health_script,
                )
            else:
                await _run_cmd(
                    project_id,
                    "docker", "compose", "-f", compose_file,
                    "up", "-d", "--no-deps", service,
                )
                # 健康检查 / Health check
                _broadcast_step(project_id, "health")
                _broadcast(project_id, "==> [健康检查] 等待 20 秒...")
                await asyncio.sleep(20)
                status = await _get_docker_status(service)
                if "Up" not in status:
                    _broadcast(project_id, f"==> [健康检查] 容器状态异常（{status}），执行回滚...")
                    await _run_cmd(project_id, "docker", "tag", rollback_tag, image)
                    await _run_cmd(
                        project_id,
                        "docker", "compose", "-f", compose_file,
                        "up", "-d", "--no-deps", service,
                    )
                    raise RuntimeError(f"新容器启动失败（{status}），已自动回滚到旧版本")
                _broadcast(project_id, f"==> [健康检查] 容器运行正常 ✓（{status}）")
                _broadcast_step(project_id, "post_deploy")
                await _run_hook(project_id, "post_deploy", tmp_dir, hook_env)
            # 清理 dangling 镜像 / Clean up dangling images
            _broadcast(project_id, "==> [清理] docker image prune -f")
            await _run_cmd(project_id, "docker", "image", "prune", "-f")
        ps.phase = "done"

    except Exception as e:
        ps.phase = "error"
        ps.error = str(e)
        _broadcast(project_id, f"ERROR: {e}")

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        finished = datetime.now()
        ps.finished_at = finished
        _pre_persist_result(project_id, started, ps)
        _broadcast(project_id, "__done__")
        _tasks.pop(project_id, None)


def _pre_persist_result(project_id: str, started: datetime, ps):
    """将部署结果写入 deploy_projects.json，供容器重启后恢复。
    Persist deploy result to deploy_projects.json for recovery after restart.
    """
    finished = datetime.now()
    duration = round((finished - started).total_seconds())
    if not ps.history or ps.history[0].get("time") != started.isoformat():
        ps.history.insert(0, {
            "time": started.isoformat(),
            "duration": duration,
            "success": ps.phase == "done",
        })
        ps.history = ps.history[:5]
    projects = load_deploy_projects()
    for p in projects:
        if p["id"] == project_id:
            p["last_phase"] = ps.phase
            p["last_error"] = ps.error
            p["last_finished_at"] = finished.isoformat()
            break
    _save_projects(projects)


# ── HTML helpers / HTML 辅助 ─────────────────────────────────────────────────

_PHASE_LABELS = {
    "idle":       ("IDLE",   "#64748b"),
    "pulling":    ("拉取中…",  "#f59e0b"),
    "building":   ("构建中…",  "#3b82f6"),
    "restarting": ("重启中…",  "#8b5cf6"),
    "done":       ("完成",    "#22c55e"),
    "error":      ("失败",    "#dc2626"),
}



# 步骤索引映射（与 JS _STEP_IDX 保持一致）
# Step index mapping (matches JS _STEP_IDX)
_STEP_IDX_PY = {
    "clone": 0, "backup": 0,
    "pre_build": 1, "build": 1, "post_build": 1,
    "pre_deploy": 2, "compose_up": 2, "post_deploy": 2,
    "health": 3,
}
_BAR_LABELS_PY = ["Clone", "Build", "Deploy", "验证", "完成"]


def _render_steps_bar(pid: str, ps) -> str:
    """生成初始进度条 HTML。
    Render initial steps bar HTML.
    """
    step = ps.step
    phase = ps.phase
    idx = _STEP_IDX_PY.get(step, -1)
    items = []
    for i, label in enumerate(_BAR_LABELS_PY):
        if phase == "done":
            cls, dot = "done", "&#10003;"
        elif phase == "error":
            if i < idx:       cls, dot = "done",    "&#10003;"
            elif i == idx:    cls, dot = "error",   "&#10007;"
            else:             cls, dot = "pending", ""
        elif idx < 0:
            cls, dot = "pending", ""
        else:
            if i < idx:       cls, dot = "done",   "&#10003;"
            elif i == idx:    cls, dot = "active",  ""
            else:             cls, dot = "pending", ""
        items.append(
            f'<div class="step-item {cls}">'
            f'<div class="step-dot">{dot}</div>'
            f'<div class="step-lbl">{label}</div>'
            f'</div>'
        )
    return f'<div class="steps-bar" id="steps-{pid}">{"".join(items)}</div>'


def _last_info_html(p: dict, ps) -> str:
    """生成「最后部署」字段 HTML。
    Render last deploy info HTML.
    """
    action = "发布" if p.get("project_type") == "frontend" else "部署"
    if ps.finished_at:
        ts = int(ps.finished_at.timestamp())
        ok = ps.phase == "done"
        rc, rv = ("#22c55e", f"✓ {action}成功") if ok else ("#dc2626", f"✗ {action}失败")
        return f'<span class="rt" data-ts="{ts}">—</span>&nbsp;<span style="color:{rc};font-weight:600">{rv}</span>'
    if p.get("last_finished_at"):
        try:
            ft = datetime.fromisoformat(p["last_finished_at"])
            ts = int(ft.timestamp())
            ok = p.get("last_phase") == "done"
            rc, rv = ("#22c55e", f"✓ {action}成功") if ok else ("#dc2626", f"✗ {action}失败")
            return f'<span class="rt" data-ts="{ts}">—</span>&nbsp;<span style="color:{rc};font-weight:600">{rv}</span>'
        except Exception:
            pass
    busy = ps.phase in ("pulling", "building", "restarting")
    if ps.started_at and busy:
        ts = int(ps.started_at.timestamp())
        return f'<span class="rt" data-ts="{ts}">—</span>'
    return "—"


def _enrich_project(p: dict) -> dict:
    """为模板渲染预计算工程附加属性。
    Pre-compute extra attributes on project dict for template rendering.
    """
    pid = p["id"]
    ps = state.deploy.get_project(pid)

    # JSON 数据（排除 token）/ JSON data (exclude token)
    p["_json"] = json.dumps(
        {k: v for k, v in p.items() if k != "github_token" and not k.startswith("_")},
        ensure_ascii=False,
    ).replace("'", "&#39;")

    # 最后部署信息 HTML / Last deploy info HTML
    p["_last_info"] = _last_info_html(p, ps)

    # 进度条 HTML / Steps bar HTML
    p["_steps_bar"] = _render_steps_bar(pid, ps)

    # 日志内容 / Log content
    if ps.logs:
        p["_log_content"] = "\n".join(ps.logs)
    else:
        lf = _log_file(pid)
        p["_log_content"] = lf.read_text(encoding="utf-8").rstrip() if lf.exists() else ""

    # last_finished_at 时间戳（供模板使用）/ Timestamp for template
    if p.get("last_finished_at"):
        try:
            p["last_finished_at_ts"] = int(datetime.fromisoformat(p["last_finished_at"]).timestamp())
        except Exception:
            p["last_finished_at_ts"] = 0

    return p


@deploy_router.get("/", response_class=HTMLResponse)
async def deploy_page():
    """部署管理页面（Jinja2 模板渲染）。
    Deploy management page (Jinja2 template rendering).
    """
    projects = load_deploy_projects()
    busy_ids = set(pid for pid, t in _tasks.items() if not t.done())

    # 为每个工程预计算模板所需属性 / Pre-compute template attributes
    for p in projects:
        _enrich_project(p)

    # 构建部署状态字典（模板中通过 deploy_states[pid] 访问）
    # Build deploy states dict (accessed via deploy_states[pid] in templates)
    deploy_states = {p["id"]: state.deploy.get_project(p["id"]) for p in projects}

    # 初始化脚本 / Initialization scripts
    proj_init_js = "\n".join(
        f"connectSSE('{p['id']}');" if p["id"] in busy_ids else f"initProject('{p['id']}');"
        for p in projects
    )
    auto_select_js = f"selectProject('{projects[0]['id']}');" if projects else ""

    tmpl = _templates.get_template("deploy.html")
    return tmpl.render(
        projects=projects,
        deploy_states=deploy_states,
        phase_labels=_PHASE_LABELS,
        default_compose=settings.DEPLOY_COMPOSE_FILE,
        github_token_set=bool(settings.DEPLOY_GITHUB_TOKEN),
        compose_file=settings.DEPLOY_COMPOSE_FILE,
        spec_html=_SPEC_HTML,
        proj_init_js=proj_init_js,
        auto_select_js=auto_select_js,
    )


# ── REST API ──────────────────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    """创建工程请求体。
    Create project request body.
    """
    name: str
    repo_url: str
    github_token: str = ""
    service_name: str = ""
    image_name: str = ""
    compose_file: str = ""
    self_service: bool = False
    project_type: str = ""          # ""=通用, "python"=Python, "java"=Java, "frontend"=前端
    build_command: str = ""         # 自定义构建命令 / Custom build command
    # Java 专用字段 / Java-only fields
    java_version: str = ""          # Java 版本，默认 17 / Java version, default 17
    # 前端专用字段 / Frontend-only fields
    app_dir: str = ""               # 前端代码所在子目录 / Frontend source subdirectory
    dist_dir: str = ""              # 产物目录，默认 dist / Build output dir, default dist
    nginx_container: str = ""       # nginx 容器名 / nginx container name
    deploy_target_dir: str = ""     # 目标路径 / Target path
    node_version: str = ""          # node 版本，默认 lts / Node version, default lts


class ProjectUpdate(BaseModel):
    """更新工程请求体。
    Update project request body.
    """
    name: str
    repo_url: str
    github_token: str = ""
    service_name: str = ""
    image_name: str = ""
    compose_file: str = ""
    self_service: bool = False
    project_type: str = ""
    build_command: str = ""
    java_version: str = ""
    app_dir: str = ""
    dist_dir: str = ""
    nginx_container: str = ""
    deploy_target_dir: str = ""
    node_version: str = ""


@deploy_router.post("/projects")
async def create_project(body: ProjectCreate):
    """添加新工程。
    Add a new project.
    """
    projects = load_deploy_projects()
    project = {
        "id": str(uuid.uuid4()),
        "name": body.name,
        "repo_url": body.repo_url,
        "github_token": body.github_token,
        "service_name": body.service_name,
        "image_name": body.image_name,
        "compose_file": body.compose_file,
        "self_service": body.self_service,
        "project_type": body.project_type,
        "build_command": body.build_command,
        "java_version": body.java_version,
        "app_dir": body.app_dir,
        "dist_dir": body.dist_dir,
        "nginx_container": body.nginx_container,
        "deploy_target_dir": body.deploy_target_dir,
        "node_version": body.node_version,
    }
    projects.append(project)
    _save_projects(projects)
    return JSONResponse({"ok": True, "id": project["id"]})


@deploy_router.put("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate):
    """更新工程配置。
    Update project configuration.
    """
    projects = load_deploy_projects()
    for p in projects:
        if p["id"] == project_id:
            p["name"] = body.name
            p["repo_url"] = body.repo_url
            if body.github_token:
                p["github_token"] = body.github_token
            p["service_name"] = body.service_name
            p["image_name"] = body.image_name
            p["compose_file"] = body.compose_file
            p["self_service"] = body.self_service
            p["project_type"] = body.project_type
            p["build_command"] = body.build_command
            p["java_version"] = body.java_version
            p["app_dir"] = body.app_dir
            p["dist_dir"] = body.dist_dir
            p["nginx_container"] = body.nginx_container
            p["deploy_target_dir"] = body.deploy_target_dir
            p["node_version"] = body.node_version
            _save_projects(projects)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "工程不存在")


@deploy_router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除工程。
    Delete a project.
    """
    projects = load_deploy_projects()
    new_list = [p for p in projects if p["id"] != project_id]
    if len(new_list) == len(projects):
        raise HTTPException(404, "工程不存在")
    _save_projects(new_list)
    return JSONResponse({"ok": True})


@deploy_router.post("/projects/{project_id}/trigger")
async def trigger_deploy(project_id: str):
    """触发部署。
    Trigger deployment.
    """
    project = _find_project(project_id)
    if not project:
        raise HTTPException(404, "工程不存在")
    if project_id in _tasks and not _tasks[project_id].done():
        raise HTTPException(409, "该工程已有部署任务在执行中")
    state.deploy.get_project(project_id).phase = "pulling"
    _tasks[project_id] = asyncio.create_task(_deploy_pipeline(project_id, project))
    return JSONResponse({"ok": True})


@deploy_router.post("/projects/{project_id}/rollback")
async def rollback_deploy(project_id: str):
    """手动回滚：将 :rollback 镜像重新部署为当前版本。
    Manual rollback: redeploy the :rollback image as the current version.
    """
    project = _find_project(project_id)
    if not project:
        raise HTTPException(404, "工程不存在")
    if project_id in _tasks and not _tasks[project_id].done():
        raise HTTPException(409, "该工程正在部署，无法回滚")

    image = project.get("image_name") or f"{project['service_name']}:latest"
    image_base = image.rsplit(":", 1)[0]
    rollback_tag = f"{image_base}:rollback"
    compose_file = project.get("compose_file") or settings.DEPLOY_COMPOSE_FILE
    service = project["service_name"]

    check = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", rollback_tag,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await check.wait()
    if check.returncode != 0:
        raise HTTPException(404, f"回滚镜像 {rollback_tag} 不存在，尚未有成功部署记录")

    async def _rollback_task():
        ps = state.deploy.get_project(project_id)
        ps.phase = "restarting"
        ps.step = "idle"
        ps.logs = []
        ps.error = None
        _broadcast_step(project_id, "compose_up")
        _broadcast(project_id, f"==> [回滚] 恢复镜像 {rollback_tag} → {image}")
        await _run_cmd(project_id, "docker", "tag", rollback_tag, image)
        _broadcast(project_id, f"==> [回滚] docker compose up ({service})")
        rc = await _run_cmd(
            project_id,
            "docker", "compose", "-f", compose_file,
            "up", "-d", "--no-deps", service,
        )
        if rc != 0:
            ps.phase = "error"
            ps.error = "回滚失败"
            _broadcast(project_id, "ERROR: 回滚失败")
        else:
            ps.phase = "done"
            _broadcast(project_id, "==> [回滚] 完成 ✓")
        ps.finished_at = datetime.now()
        _broadcast(project_id, "__done__")
        _tasks.pop(project_id, None)

    _tasks[project_id] = asyncio.create_task(_rollback_task())
    return JSONResponse({"ok": True})


@deploy_router.get("/projects/{project_id}/status")
async def project_status(project_id: str):
    """获取工程部署状态。
    Get project deploy status.
    """
    project = _find_project(project_id)
    ps = state.deploy.get_project(project_id)
    docker_status = await _get_docker_status(project["service_name"]) if project else "—"
    return {
        "project_type": (project or {}).get("project_type", ""),
        "last_phase": (project or {}).get("last_phase"),
        "last_finished_at": (project or {}).get("last_finished_at"),
        "phase": ps.phase,
        "step": ps.step,
        "started_at": ps.started_at.isoformat() if ps.started_at else None,
        "finished_at": ps.finished_at.isoformat() if ps.finished_at else None,
        "error": ps.error,
        "docker_status": docker_status,
        "history": ps.history,
    }


@deploy_router.get("/projects/{project_id}/logs")
async def project_logs_file(project_id: str):
    """获取工程部署日志文件。
    Get project deploy log file.
    """
    from fastapi.responses import PlainTextResponse
    f = _log_file(project_id)
    return PlainTextResponse(f.read_text(encoding="utf-8") if f.exists() else "")


@deploy_router.get("/projects/{project_id}/stream")
async def project_stream(project_id: str):
    """SSE 实时日志流。
    Server-Sent Events real-time log stream.
    """
    ps = state.deploy.get_project(project_id)
    existing = list(ps.logs)
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[project_id].append(q)

    async def generator():
        try:
            for line in existing:
                yield f"data: {line}\n\n"
            if project_id not in _tasks or _tasks[project_id].done():
                yield "data: __done__\n\n"
                return
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {line}\n\n"
                    if line == "__done__":
                        break
                except asyncio.TimeoutError:
                    if project_id not in _tasks or _tasks[project_id].done():
                        yield "data: __done__\n\n"
                        break
                    yield ": heartbeat\n\n"
        finally:
            if q in _subscribers[project_id]:
                _subscribers[project_id].remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Config update API / 配置更新接口 ─────────────────────────────────────────

from fastapi import Form as _Form


@deploy_router.post("/config/update", response_class=JSONResponse)
async def update_config_form(key: str = _Form(""), value: str = _Form("")):
    """通过表单更新全局配置。
    Update global config via form data.
    """
    allowed_keys = {"DEPLOY_GITHUB_TOKEN", "DEPLOY_COMPOSE_FILE"}
    if key not in allowed_keys:
        raise HTTPException(400, f"不允许修改的配置项: {key}")
    settings.update(key, value)
    return {"ok": True}
