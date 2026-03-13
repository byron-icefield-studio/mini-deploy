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
from pydantic import BaseModel

import state
from config import settings

deploy_router = APIRouter(prefix="/deploy")

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

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh}
nav{background:#1e293b;padding:.7rem 1.5rem;display:flex;align-items:center;gap:.75rem}
.back-btn{color:#94a3b8;text-decoration:none;font-size:.85rem;padding:.3rem .7rem;border:1px solid #334155;border-radius:6px;transition:all .15s}
.back-btn:hover{color:#fff;background:#334155}
.nav-title{color:#94a3b8;font-size:.85rem;flex:1}
.add-btn{padding:.35rem .9rem;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:500;flex-shrink:0}
.add-btn:hover{background:#1d4ed8}
.page{padding:1.25rem 1.5rem;width:100%;max-width:none;min-height:calc(100vh - 54px)}
.card{background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.07);border:1px solid #e2e8f0;overflow:hidden;margin-bottom:1rem}
.card-h{padding:.85rem 1rem;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.card-h h2{font-size:.9rem;font-weight:600}
.card-b{padding:1rem}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;display:inline-block}
.badge{font-size:.72rem;padding:.15rem .5rem;border-radius:20px;font-weight:600;white-space:nowrap}
.proj-name{font-weight:600;font-size:.92rem;flex:1;min-width:0}
.proj-actions{display:flex;gap:.35rem;flex-shrink:0}
.act-btn{padding:.2rem .6rem;font-size:.74rem;font-weight:600;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0;border-radius:5px;cursor:pointer;white-space:nowrap;transition:all .15s}
.act-btn:hover{background:#e2e8f0}
.act-btn:disabled{opacity:.5;cursor:not-allowed}
.act-btn-primary{background:#dbeafe;color:#1d4ed8;border-color:#bfdbfe}
.act-btn-primary:hover{background:#bfdbfe}
.act-btn-danger{background:#fef2f2;color:#dc2626;border-color:#fecaca}
.act-btn-danger:hover{background:#fee2e2}
.proj-meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.65rem}
.meta-row{display:flex;flex-direction:column;font-size:.83rem;gap:.25rem;padding:.55rem .65rem;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;min-width:0}
.ml{color:#94a3b8;flex-shrink:0;font-size:.74rem}
.mv{color:#334155;font-weight:500;overflow-wrap:anywhere;word-break:break-word;white-space:normal;max-width:none;font-size:.83rem}
.mv-mono{font-family:monospace;font-size:.78rem}
.log-wrap{border-top:1px solid #f1f5f9}
.log-box{background:#0f172a;color:#e2e8f0;padding:.9rem 1rem;font-family:monospace;font-size:.78rem;line-height:1.6;height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:.65rem}
.form-row{display:flex;flex-direction:column;gap:.25rem}
.form-row label{font-size:.78rem;font-weight:600;color:#475569}
.form-row input{padding:.4rem .65rem;border:1px solid #e2e8f0;border-radius:6px;font-size:.84rem;width:100%}
.form-row input:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 2px #bfdbfe50}
.form-row select{padding:.4rem .65rem;border:1px solid #e2e8f0;border-radius:6px;font-size:.84rem;width:100%;background:#fff;color:#1e293b;cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2394a3b8' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right .65rem center}
.form-row select:focus{outline:none;border-color:#3b82f6;box-shadow:0 0 0 2px #bfdbfe50}
.form-hint{font-size:.72rem;color:#94a3b8;margin-top:.1rem}
.checkbox-label{display:flex;align-items:center;gap:.5rem;font-size:.85rem;color:#475569;cursor:pointer}
.checkbox-label input[type=checkbox]{width:15px;height:15px;cursor:pointer}
.form-full{grid-column:1/-1}
.form-actions{grid-column:1/-1;display:flex;gap:.5rem;margin-top:.15rem}
.primary-btn{padding:.4rem 1.2rem;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:.84rem;font-weight:500}
.primary-btn:hover{background:#1d4ed8}
.cancel-btn{padding:.4rem .9rem;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0;border-radius:6px;cursor:pointer;font-size:.84rem}
.empty-state{text-align:center;padding:3.5rem 1rem;color:#94a3b8}
.empty-text{font-size:.95rem;font-weight:600;margin:.4rem 0 .25rem;color:#64748b}
.empty-hint{font-size:.82rem}
details.card summary{list-style:none;padding:.85rem 1rem;font-size:.88rem;font-weight:600;cursor:pointer;user-select:none;color:#475569}
details.card summary::-webkit-details-marker{display:none}
details.card summary::after{content:" ↓";font-size:.75rem;color:#94a3b8}
details[open].card summary::after{content:" ↑";font-size:.75rem}
.spec-section{margin-bottom:1rem;padding-bottom:1rem;border-bottom:1px solid #f1f5f9}
.spec-section:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.spec-title{font-size:.82rem;font-weight:600;color:#1e293b;margin-bottom:.3rem}
.spec-title code{background:#f1f5f9;padding:.1rem .35rem;border-radius:4px;font-size:.78rem;color:#6366f1}
.spec-desc{font-size:.8rem;color:#64748b;margin-bottom:.5rem}
.spec-code{background:#0f172a;color:#e2e8f0;border-radius:7px;padding:.75rem 1rem;font-family:monospace;font-size:.76rem;line-height:1.6;overflow-x:auto;white-space:pre}
.spec-tbl{width:100%;border-collapse:collapse;font-size:.8rem}
.spec-tbl tr{border-bottom:1px solid #f8fafc}
.spec-tbl tr:last-child{border-bottom:none}
.spec-tbl td{padding:.35rem .5rem;vertical-align:top}
.spec-tbl td:first-child code{background:#f1f5f9;padding:.1rem .35rem;border-radius:4px;color:#6366f1;font-size:.78rem;white-space:nowrap}
.spec-tbl td:last-child{color:#64748b}
.setting-row{display:flex;align-items:center;gap:.75rem;padding:.6rem 0;border-bottom:1px solid #f8fafc;flex-wrap:wrap}
.setting-row:last-child{border-bottom:none}
.setting-label{font-size:.82rem;color:#475569;font-weight:500;width:140px;flex-shrink:0}
.setting-form{display:flex;align-items:center;gap:.4rem;flex:1;min-width:0}
.setting-form input{flex:1;min-width:0;padding:.35rem .6rem;border:1px solid #e2e8f0;border-radius:6px;font-size:.83rem}
.setting-form input:focus{outline:none;border-color:#3b82f6}
.setting-save-btn{padding:.3rem .75rem;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:.8rem;white-space:nowrap;flex-shrink:0}
.setting-save-btn:hover{background:#1d4ed8}
.setting-saved{font-size:.75rem;color:#16a34a;display:none}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:100}
.modal-card{background:#fff;border-radius:10px;width:min(620px,92vw);max-height:88vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.25)}
.modal-h{padding:.85rem 1rem;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;justify-content:space-between}
.modal-h h2{font-size:.9rem;font-weight:600}
.modal-close{background:none;border:none;font-size:1.3rem;cursor:pointer;color:#94a3b8;padding:.1rem .4rem;border-radius:4px}
.modal-close:hover{color:#1e293b;background:#f1f5f9}
.steps-bar{border-top:1px solid #f1f5f9;padding:.45rem .75rem .45rem;display:flex;position:relative}
.steps-bar::before{content:'';position:absolute;top:calc(.45rem + 8px);left:calc(.75rem + 9px);right:calc(.75rem + 9px);height:2px;background:#e2e8f0;z-index:0}
.step-item{display:flex;flex-direction:column;align-items:center;flex:1;z-index:1}
.step-dot{width:18px;height:18px;border-radius:50%;border:2px solid #e2e8f0;background:#f8fafc;display:flex;align-items:center;justify-content:center;font-size:.55rem;font-weight:700;color:#fff;transition:all .25s}
.step-item.done .step-dot{background:#22c55e;border-color:#22c55e}
.step-item.active .step-dot{background:#3b82f6;border-color:#3b82f6;animation:step-pulse 1.2s infinite}
.step-item.error .step-dot{background:#dc2626;border-color:#dc2626}
.step-lbl{font-size:.6rem;color:#94a3b8;margin-top:.2rem;text-align:center;white-space:nowrap;line-height:1.2}
.step-item.done .step-lbl{color:#16a34a}
.step-item.active .step-lbl{color:#2563eb;font-weight:600}
.step-item.error .step-lbl{color:#dc2626}
@keyframes step-pulse{0%,100%{box-shadow:0 0 0 0 #3b82f650}60%{box-shadow:0 0 0 5px #3b82f620}}
/* ── 左右布局 / Left-right layout ── */
.deploy-layout{display:flex;gap:1rem;align-items:flex-start;width:100%}
.proj-sidebar{width:clamp(300px,34vw,460px);flex-shrink:0;background:#fff;border-radius:10px;border:1px solid #e2e8f0;overflow:hidden}
.sidebar-h{padding:.65rem .85rem;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;justify-content:space-between;background:#f8fafc}
.sidebar-title{font-size:.78rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em}
.sidebar-add{padding:.15rem .5rem;background:#2563eb;color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:.8rem;font-weight:600;line-height:1.5}
.sidebar-add:hover{background:#1d4ed8}
.proj-list{padding:.6rem;display:flex;flex-direction:column;gap:.55rem}
.proj-item{padding:.65rem .75rem;cursor:pointer;display:flex;flex-direction:column;gap:.35rem;border:1px solid #e2e8f0;border-radius:9px;transition:all .12s;min-width:0;background:#fff}
.proj-item:hover{background:#f8fafc;border-color:#cbd5e1}
.proj-item.selected{background:#eff6ff;border-color:#93c5fd;box-shadow:inset 3px 0 0 #3b82f6}
.item-row1{display:flex;align-items:flex-start;gap:.45rem;width:100%;min-width:0}
.item-name{flex:1;font-size:.86rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.item-status{font-size:.64rem;font-weight:600;padding:.1rem .38rem;border-radius:10px;white-space:nowrap;flex-shrink:0;max-width:70px;overflow:hidden;text-overflow:ellipsis}
.item-row2{display:flex;gap:.55rem;flex-wrap:wrap}
.item-kv{font-size:.72rem;color:#64748b;background:#f8fafc;border:1px solid #e2e8f0;padding:.1rem .35rem;border-radius:999px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-meta{font-size:.72rem;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%;line-height:1.35}
.sidebar-empty{padding:1.2rem .85rem;color:#94a3b8;font-size:.8rem;text-align:center}
.spec-nav-btn{padding:.3rem .65rem;background:transparent;color:#94a3b8;border:1px solid #475569;border-radius:6px;cursor:pointer;font-size:.8rem;white-space:nowrap;flex-shrink:0}
.spec-nav-btn:hover{color:#fff;background:#334155}
.spec-modal-card{width:min(860px,95vw)}
.proj-detail-wrap{flex:1;min-width:0}
.detail-empty{background:#fff;border-radius:10px;border:1px solid #e2e8f0;color:#94a3b8;text-align:center;padding:5rem 1rem;font-size:.88rem}
.proj-detail{background:#fff;border-radius:10px;border:1px solid #e2e8f0;overflow:hidden}
.detail-h{padding:.85rem 1rem;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.detail-name{font-weight:600;font-size:.92rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:980px){
  nav{padding:.7rem 1rem;flex-wrap:wrap}
  .page{padding:1rem}
  .deploy-layout{flex-direction:column}
  .proj-sidebar{width:100%}
  .proj-detail-wrap{width:100%}
}
@media (max-width:720px){
  .form-grid{grid-template-columns:1fr}
  .form-actions{flex-wrap:wrap}
  .proj-actions{width:100%;justify-content:flex-start;flex-wrap:wrap}
  .detail-h{align-items:flex-start}
  .item-name{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
}
"""

_JS = r"""
// 步骤 → 进度条索引映射（5步：Clone / Build / Deploy / 验证 / 完成）
// Step-to-bar-index map: Clone=0 Build=1 Deploy=2 Health=3 Done=4
const _STEP_IDX = {
  clone:0, backup:0,
  pre_build:1, build:1, post_build:1,
  pre_deploy:2, compose_up:2, post_deploy:2,
  health:3
};
const _BAR_LABELS = ['Clone','Build','Deploy','验证','完成'];

function updateStepsBar(id, step, phase) {
  const bar = document.getElementById('steps-' + id);
  if (!bar) return;
  const items = bar.querySelectorAll('.step-item');
  const idx = (step in _STEP_IDX) ? _STEP_IDX[step] : -1;
  items.forEach((el, i) => {
    const dot = el.querySelector('.step-dot');
    el.className = 'step-item';
    if (phase === 'done') {
      el.classList.add('done'); dot.textContent = '✓';
    } else if (phase === 'error') {
      if (i < idx)      { el.classList.add('done');    dot.textContent = '✓'; }
      else if (i === idx){ el.classList.add('error');  dot.textContent = '✗'; }
      else               { el.classList.add('pending'); dot.textContent = ''; }
    } else if (idx < 0) {
      el.classList.add('pending'); dot.textContent = '';
    } else {
      if (i < idx)      { el.classList.add('done');   dot.textContent = '✓'; }
      else if (i === idx){ el.classList.add('active'); dot.textContent = ''; }
      else               { el.classList.add('pending'); dot.textContent = ''; }
    }
  });
}

function relTime(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60)    return d + '秒前';
  if (d < 3600)  return Math.floor(d/60) + '分钟前';
  if (d < 86400) return Math.floor(d/3600) + '小时前';
  return Math.floor(d/86400) + '天前';
}
function updateTimes() {
  document.querySelectorAll('.rt[data-ts]').forEach(el => el.textContent = relTime(+el.dataset.ts));
}
updateTimes();
setInterval(updateTimes, 5000);

function openAddModal() {
  document.getElementById('add-modal').style.display = '';
  setTimeout(() => { const f = document.getElementById('add-modal').querySelector('input'); if(f) f.focus(); }, 50);
}
function closeAddModal() {
  document.getElementById('add-modal').style.display = 'none';
}

function selectProject(id) {
  document.querySelectorAll('.proj-item').forEach(el => el.classList.remove('selected'));
  const item = document.getElementById('item-' + id);
  if (item) item.classList.add('selected');
  document.querySelectorAll('.proj-detail').forEach(el => el.style.display = 'none');
  const detail = document.getElementById('proj-' + id);
  if (detail) detail.style.display = '';
  const emp = document.getElementById('detail-empty');
  if (emp) emp.style.display = 'none';
}

async function submitAddForm(event) {
  event.preventDefault();
  const fd = new FormData(event.target);
  const data = Object.fromEntries(fd.entries());
  data.self_service = fd.has('self_service');
  const r = await fetch('/deploy/projects', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (r.ok) {
    location.reload();
  } else {
    const e = await r.json();
    alert('添加失败: ' + (e.detail || '未知错误'));
  }
}

async function deleteProject(id) {
  if (!confirm('确认删除此工程？')) return;
  const r = await fetch('/deploy/projects/' + id, {method: 'DELETE'});
  if (r.ok) {
    const item = document.getElementById('item-' + id);
    if (item) item.remove();
    const detail = document.getElementById('proj-' + id);
    if (detail) detail.remove();
    const list = document.getElementById('proj-list');
    if (list && !list.querySelector('.proj-item')) {
      list.innerHTML = '<div class="sidebar-empty">还没有工程</div>';
    }
    document.querySelectorAll('.proj-detail').forEach(el => el.style.display = 'none');
    const emp = document.getElementById('detail-empty');
    if (emp) emp.style.display = '';
  }
}

async function triggerDeploy(id, btn) {
  if (!confirm('确认触发部署？\ngit clone → build → docker compose up')) return;
  btn.disabled = true;
  btn.textContent = '部署中…';
  const rb = document.getElementById('rollback-btn-' + id);
  if (rb) rb.disabled = true;
  const logbox = document.getElementById('logbox-' + id);
  if (logbox) logbox.textContent = '';
  const r = await fetch('/deploy/projects/' + id + '/trigger', {method: 'POST'});
  if (r.status === 409) {
    alert('该工程已有部署任务在执行中');
    btn.disabled = false;
    btn.textContent = '部署';
    if (rb) rb.disabled = false;
    return;
  }
  connectSSE(id);
}

async function triggerRollback(id, btn) {
  if (!confirm('确认回滚？将恢复上次成功部署的镜像版本。')) return;
  const depBtn = document.getElementById('deploy-btn-' + id);
  if (depBtn) depBtn.disabled = true;
  btn.disabled = true;
  btn.textContent = '回滚中…';
  const logbox = document.getElementById('logbox-' + id);
  if (logbox) logbox.textContent = '';
  const r = await fetch('/deploy/projects/' + id + '/rollback', {method: 'POST'});
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    alert('回滚失败: ' + (e.detail || r.status));
    btn.disabled = false;
    btn.textContent = '回滚';
    if (depBtn) { depBtn.disabled = false; depBtn.textContent = '部署'; }
    return;
  }
  connectSSE(id);
}

const _sse = {};
const _watchTimers = {};
function connectSSE(id) {
  if (_sse[id]) { _sse[id].close(); delete _sse[id]; }
  const es = new EventSource('/deploy/projects/' + id + '/stream');
  _sse[id] = es;
  const logbox = document.getElementById('logbox-' + id);
  es.onmessage = e => {
    if (e.data === '__done__') {
      es.close(); delete _sse[id];
      pollStatus(id);
      return;
    }
    if (e.data.startsWith('__step:')) {
      const step = e.data.slice(7, -2);
      updateStepsBar(id, step, 'running');
      return;
    }
    if (logbox) {
      logbox.textContent += (logbox.textContent ? '\n' : '') + e.data;
      logbox.scrollTop = logbox.scrollHeight;
    }
  };
  es.onerror = async () => {
    es.close(); delete _sse[id];
    try {
      const d = await pollStatus(id);
      const busy = d && ['pulling','building','restarting'].includes(d.phase);
      if (busy) {
        setTimeout(() => connectSSE(id), 1500);
      }
    } catch(e) {}
  };
}

const _phaseLabels = {idle:'IDLE',pulling:'拉取中…',building:'构建中…',restarting:'重启中…',done:'完成',error:'失败'};
const _phaseColors = {idle:'#94a3b8',pulling:'#f59e0b',building:'#3b82f6',restarting:'#8b5cf6',done:'#22c55e',error:'#dc2626'};
function _dockerColor(s) {
  if (!s || s === '—') return '#94a3b8';
  if (s.startsWith('Up')) return '#22c55e';
  if (s.startsWith('Exited')) return '#64748b';
  if (s.startsWith('Restarting')) return '#f59e0b';
  return '#94a3b8';
}

async function saveSetting(event, key) {
  event.preventDefault();
  const form = event.target;
  const fd = new FormData(form);
  const value = fd.get('value') || '';
  const isPassword = form.querySelector('input[type="password"]') !== null;
  if (isPassword && value === '') return;
  const body = new FormData();
  body.append('key', key);
  body.append('value', value);
  const r = await fetch('/config/update', {method: 'POST', body});
  if (r.ok) {
    const saved = form.querySelector('.setting-saved');
    if (saved) { saved.style.display = 'inline'; setTimeout(() => saved.style.display = 'none', 2000); }
  }
}

function onProjectTypeChange(sel) {
  const form = sel.closest('form');
  const isFe = sel.value === 'frontend';
  const isJava = sel.value === 'java';
  form.querySelectorAll('[data-docker-only]').forEach(el => el.style.display = isFe ? 'none' : '');
  form.querySelectorAll('[data-frontend-only]').forEach(el => el.style.display = isFe ? '' : 'none');
  form.querySelectorAll('[data-java-only]').forEach(el => el.style.display = isJava ? '' : 'none');
}

function editProject(id) {
  const card = document.getElementById('proj-' + id);
  const p = JSON.parse(card.dataset.project);
  document.getElementById('edit-id').value = p.id;
  document.getElementById('edit-name').value = p.name || '';
  document.getElementById('edit-repo-url').value = p.repo_url || '';
  document.getElementById('edit-github-token').value = '';
  document.getElementById('edit-service-name').value = p.service_name || '';
  document.getElementById('edit-image-name').value = p.image_name || '';
  document.getElementById('edit-compose-file').value = p.compose_file || '';
  document.getElementById('edit-self-service').checked = !!p.self_service;
  const ptSel = document.getElementById('edit-project-type');
  ptSel.value = p.project_type || '';
  document.getElementById('edit-build-command').value = p.build_command || '';
  document.getElementById('edit-app-dir').value = p.app_dir || '';
  document.getElementById('edit-dist-dir').value = p.dist_dir || '';
  document.getElementById('edit-nginx-container').value = p.nginx_container || '';
  document.getElementById('edit-deploy-target-dir').value = p.deploy_target_dir || '';
  document.getElementById('edit-node-version').value = p.node_version || '';
  document.getElementById('edit-java-version').value = p.java_version || '';
  onProjectTypeChange(ptSel);
  document.getElementById('edit-modal').style.display = '';
}

function closeEditModal() {
  document.getElementById('edit-modal').style.display = 'none';
}

async function submitEditForm(event) {
  event.preventDefault();
  const fd = new FormData(event.target);
  const id = fd.get('_id');
  const data = Object.fromEntries(fd.entries());
  delete data._id;
  data.self_service = fd.has('self_service');
  const r = await fetch('/deploy/projects/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  });
  if (r.ok) {
    location.reload();
  } else {
    const e = await r.json();
    alert('保存失败: ' + (e.detail || '未知错误'));
  }
}

async function pollStatus(id) {
  try {
    const r = await fetch('/deploy/projects/' + id + '/status');
    const d = await r.json();
    const detail = document.getElementById('proj-' + id);
    const p = detail ? JSON.parse(detail.dataset.project) : {};
    const isFrontend = p.project_type === 'frontend';
    const badge  = document.getElementById('badge-' + id);
    const btn    = document.getElementById('deploy-btn-' + id);
    const rbBtn  = document.getElementById('rollback-btn-' + id);
    const isStat = document.getElementById('item-status-' + id);
    const busy   = ['pulling','building','restarting'].includes(d.phase);
    let c, label;
    if (busy) {
      c = _phaseColors[d.phase]; label = _phaseLabels[d.phase];
      if (rbBtn) rbBtn.disabled = true;
    } else if (isFrontend) {
      if (d.phase === 'done' || d.last_phase === 'done')      { c = '#22c55e'; label = '已发布'; }
      else if (d.phase === 'error' || d.last_phase === 'error'){ c = '#dc2626'; label = '发布失败'; }
      else                                                     { c = '#94a3b8'; label = '待发布'; }
      if (btn) { btn.disabled = false; btn.textContent = '部署'; }
      if (rbBtn) { rbBtn.disabled = false; rbBtn.textContent = '回滚'; }
    } else {
      const s = d.docker_status || '—';
      c = _dockerColor(s); label = s;
      if (btn) { btn.disabled = false; btn.textContent = '部署'; }
      if (rbBtn) { rbBtn.disabled = false; rbBtn.textContent = '回滚'; }
    }
    if (badge) { badge.textContent = label; badge.style.color = c; badge.style.background = c + '18'; }
    if (isStat) { isStat.textContent = label; isStat.style.color = c; isStat.style.background = c + '18'; }
    if (d.step !== undefined) updateStepsBar(id, d.step, d.phase);
    if (d.finished_at) {
      const ts = Math.floor(new Date(d.finished_at).getTime() / 1000);
      const ok  = d.phase === 'done';
      const action = isFrontend ? '发布' : '部署';
      const ldEl = document.getElementById('last-deploy-' + id);
      if (ldEl) {
        const rc2 = ok ? '#22c55e' : '#dc2626';
        const rv  = ok ? `✓\u00a0${action}成功` : `✗\u00a0${action}失败`;
        ldEl.innerHTML = `<span class="rt" data-ts="${ts}">—</span>\u00a0<span style="color:${rc2};font-weight:600">${rv}</span>`;
      }
      const metaEl = document.getElementById('item-meta-' + id);
      if (metaEl) {
        const sym = ok ? '✓' : '✗';
        metaEl.innerHTML = `<span class="rt" data-ts="${ts}">—</span> ${sym}`;
      }
      updateTimes();
    }
    return d;
  } catch(e) {}
  return null;
}

async function initProject(id) {
  const logbox = document.getElementById('logbox-' + id);
  if (logbox && !logbox.textContent.trim()) {
    try {
      const r = await fetch('/deploy/projects/' + id + '/logs');
      const text = await r.text();
      if (text.trim()) {
        logbox.textContent = text.trimEnd();
        logbox.scrollTop = logbox.scrollHeight;
      }
    } catch(e) {}
  }
  await pollStatus(id);
  if (_watchTimers[id]) clearInterval(_watchTimers[id]);
  _watchTimers[id] = setInterval(async () => {
    const d = await pollStatus(id);
    const busy = d && ['pulling','building','restarting'].includes(d.phase);
    if (busy && !_sse[id]) connectSSE(id);
  }, 3000);
}
"""


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


def _sidebar_item(p: dict) -> str:
    """左侧工程列表项。
    Left sidebar project item.
    """
    pid = p["id"]
    ps = state.deploy.get_project(pid)
    busy = ps.phase in ("pulling", "building", "restarting")

    is_frontend = p.get("project_type") == "frontend"
    if busy:
        s_label, s_color = _PHASE_LABELS[ps.phase]
    elif is_frontend:
        if ps.phase == "done" or p.get("last_phase") == "done":
            s_label, s_color = "已发布", "#22c55e"
        elif ps.phase == "error" or p.get("last_phase") == "error":
            s_label, s_color = "发布失败", "#dc2626"
        else:
            s_label, s_color = "待发布", "#94a3b8"
    else:
        s_label, s_color = "—", "#94a3b8"

    meta = ""
    if ps.finished_at:
        ts = int(ps.finished_at.timestamp())
        sym = "✓" if ps.phase == "done" else "✗"
        meta = f'<span class="rt" data-ts="{ts}">—</span> {sym}'
    elif p.get("last_finished_at"):
        try:
            ft = datetime.fromisoformat(p["last_finished_at"])
            ts = int(ft.timestamp())
            sym = "✓" if p.get("last_phase") == "done" else "✗"
            meta = f'<span class="rt" data-ts="{ts}">—</span> {sym}'
        except Exception:
            pass
    if not meta:
        meta = "尚未发布" if is_frontend else "尚未部署"

    proj_type = p.get("project_type") or "generic"
    type_label = {"frontend": "前端", "python": "Python", "java": "Java", "generic": "通用"}.get(proj_type, "通用")

    return (
        f'<div class="proj-item" id="item-{pid}" onclick="selectProject(\'{pid}\')">'
        f'<div class="item-row1">'
        f'<span class="item-name">{p["name"]}</span>'
        f'<span class="item-status" id="item-status-{pid}" '
        f'style="background:{s_color}18;color:{s_color}">{s_label}</span>'
        f'</div>'
        f'<div class="item-row2">'
        f'<span class="item-kv">类型: {type_label}</span>'
        f'<span class="item-kv">服务: {p["service_name"]}</span>'
        f'</div>'
        f'<div class="item-meta" id="item-meta-{pid}">{meta}</div>'
        f'</div>'
    )


def _project_detail(p: dict) -> str:
    """右侧工程详情面板。
    Right-side project detail panel.
    """
    pid = p["id"]
    ps = state.deploy.get_project(pid)
    busy = ps.phase in ("pulling", "building", "restarting")

    is_frontend = p.get("project_type") == "frontend"
    if busy:
        init_label, init_color = _PHASE_LABELS[ps.phase]
    elif is_frontend:
        if ps.phase == "done" or p.get("last_phase") == "done":
            init_label, init_color = "已发布", "#22c55e"
        elif ps.phase == "error" or p.get("last_phase") == "error":
            init_label, init_color = "发布失败", "#dc2626"
        else:
            init_label, init_color = "待发布", "#94a3b8"
    else:
        init_label, init_color = "—", "#94a3b8"

    last_info = _last_info_html(p, ps)
    last_label = "上次发布" if is_frontend else "最后部署"
    repo_url = p["repo_url"]
    compose = p.get("compose_file") or settings.DEPLOY_COMPOSE_FILE or "（全局默认）"

    if ps.logs:
        log_content = "\n".join(ps.logs)
    else:
        lf = _log_file(pid)
        log_content = lf.read_text(encoding="utf-8").rstrip() if lf.exists() else ""

    p_json = json.dumps({k: v for k, v in p.items() if k != "github_token"}, ensure_ascii=False).replace("'", "&#39;")
    return f"""<div class="proj-detail" id="proj-{pid}" data-project='{p_json}' style="display:none">
  <div class="detail-h">
    <span class="detail-name">{p["name"]}</span>
    <span class="badge" id="badge-{pid}" style="background:{init_color}18;color:{init_color}">{init_label}</span>
    <div class="proj-actions">
      <button class="act-btn act-btn-primary" id="deploy-btn-{pid}"
        onclick="triggerDeploy('{pid}',this)"
        {'disabled' if busy else ''}>{'部署中…' if busy else '部署'}</button>
      <button class="act-btn" id="rollback-btn-{pid}"
        onclick="triggerRollback('{pid}',this)"
        title="恢复上次成功版本"
        {'disabled' if busy else ''}>回滚</button>
      <button class="act-btn" onclick="editProject('{pid}')">编辑</button>
      <button class="act-btn act-btn-danger" onclick="deleteProject('{pid}')">删除</button>
    </div>
  </div>
  <div class="card-b proj-meta">
    <div class="meta-row"><span class="ml">仓库</span><span class="mv" title="{repo_url}">{repo_url}</span></div>
    <div class="meta-row"><span class="ml">服务</span><span class="mv">{p["service_name"]}</span></div>
    <div class="meta-row"><span class="ml">Compose</span><span class="mv mv-mono" title="{compose}">{compose}</span></div>
    <div class="meta-row"><span class="ml">{last_label}</span><span class="mv" id="last-deploy-{pid}">{last_info}</span></div>
  </div>
  {_render_steps_bar(pid, ps)}
  <div class="log-wrap" id="logwrap-{pid}">
    <div class="log-box" id="logbox-{pid}">{log_content}</div>
  </div>
</div>"""


@deploy_router.get("/", response_class=HTMLResponse)
async def deploy_page():
    """部署管理页面。
    Deploy management page.
    """
    projects = load_deploy_projects()
    sidebar_items = "".join(_sidebar_item(p) for p in projects)
    detail_panels = "".join(_project_detail(p) for p in projects)
    busy_ids = set(pid for pid, t in _tasks.items() if not t.done())
    proj_init_js = "\n".join(
        f"connectSSE('{p['id']}');" if p["id"] in busy_ids else f"initProject('{p['id']}');"
        for p in projects
    )
    auto_select_js = f"selectProject('{projects[0]['id']}');" if projects else ""
    sidebar_content = sidebar_items if projects else '<div class="sidebar-empty">还没有工程</div>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mini Deploy</title>
<style>{_CSS}</style>
</head>
<body>
<nav>
  <span class="nav-title" style="color:#fff;font-weight:700">Mini Deploy</span>
  <button class="spec-nav-btn" onclick="document.getElementById('spec-modal').style.display=''">接入规范</button>
</nav>
<div class="page">

  <!-- 全局配置 / Global settings -->
  <details class="card" style="margin-bottom:1rem">
    <summary>全局配置</summary>
    <div class="card-b">
      <div class="setting-row">
        <span class="setting-label">GitHub Token</span>
        <form class="setting-form" onsubmit="saveSetting(event,'DEPLOY_GITHUB_TOKEN')">
          <input type="password" name="value" placeholder="{'已设置' if settings.DEPLOY_GITHUB_TOKEN else '未设置'}（留空不修改）">
          <button type="submit" class="setting-save-btn">保存</button>
          <span class="setting-saved">已保存 ✓</span>
        </form>
      </div>
      <div class="setting-row">
        <span class="setting-label">默认 Compose</span>
        <form class="setting-form" onsubmit="saveSetting(event,'DEPLOY_COMPOSE_FILE')">
          <input type="text" name="value" value="{settings.DEPLOY_COMPOSE_FILE}">
          <button type="submit" class="setting-save-btn">保存</button>
          <span class="setting-saved">已保存 ✓</span>
        </form>
      </div>
    </div>
  </details>

  <!-- 左右布局 / Left-right layout -->
  <div class="deploy-layout">
    <div class="proj-sidebar">
      <div class="sidebar-h">
        <span class="sidebar-title">工程列表</span>
        <button class="sidebar-add" onclick="openAddModal()" title="添加工程">+</button>
      </div>
      <div id="proj-list" class="proj-list">{sidebar_content}</div>
    </div>

    <div class="proj-detail-wrap">
      <div class="detail-empty" id="detail-empty">← 选择左侧工程查看详情</div>
      {detail_panels}
    </div>
  </div>

</div>

<!-- 接入规范弹窗 / Spec modal -->
<div id="spec-modal" class="modal-overlay" style="display:none" onclick="if(event.target===this)this.style.display='none'">
  <div class="modal-card spec-modal-card">
    <div class="modal-h">
      <h2>项目接入规范</h2>
      <button class="modal-close" onclick="document.getElementById('spec-modal').style.display='none'">×</button>
    </div>
    <div class="card-b">
      {_SPEC_HTML}
    </div>
  </div>
</div>

<!-- 添加工程弹窗 / Add project modal -->
<div id="add-modal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeAddModal()">
  <div class="modal-card">
    <div class="modal-h">
      <h2>添加工程</h2>
      <button class="modal-close" onclick="closeAddModal()">×</button>
    </div>
    <div class="card-b">
      <form onsubmit="submitAddForm(event)">
        <div class="form-grid">
          <div class="form-row">
            <label>工程名称 *</label>
            <input name="name" required placeholder="My Project">
          </div>
          <div class="form-row">
            <label>服务名 *</label>
            <input name="service_name" required placeholder="my-service">
            <span class="form-hint">docker-compose 中的服务名</span>
          </div>
          <div class="form-row form-full">
            <label>仓库地址 *</label>
            <input name="repo_url" required placeholder="https://github.com/user/repo.git">
          </div>
          <div class="form-row">
            <label>GitHub Token</label>
            <input name="github_token" type="password" placeholder="留空则使用全局 Token">
          </div>
          <div class="form-row">
            <label>工程类型</label>
            <select name="project_type" onchange="onProjectTypeChange(this)">
              <option value="">通用</option>
              <option value="python">Python</option>
              <option value="java">Java</option>
              <option value="frontend">前端</option>
            </select>
            <span class="form-hint">Python: base stage 缓存; Java: Maven/Gradle 构建; 前端: npm → nginx</span>
          </div>
          <div class="form-row" data-docker-only>
            <label>镜像名</label>
            <input name="image_name" placeholder="myapp:latest">
            <span class="form-hint">留空则用「服务名:latest」</span>
          </div>
          <div class="form-row form-full" data-docker-only>
            <label>Compose 路径</label>
            <input name="compose_file" placeholder="/pi-cluster/docker-compose.yml">
            <span class="form-hint">留空使用全局默认配置</span>
          </div>
          <div class="form-row form-full" data-docker-only>
            <label class="checkbox-label">
              <input type="checkbox" name="self_service">
              <span>自重启服务（当前工程部署自身，step 3 通过独立容器执行）</span>
            </label>
          </div>
          <div class="form-row" data-docker-only>
            <label>自定义构建命令</label>
            <input name="build_command" placeholder="留空使用默认">
            <span class="form-hint">Java: 默认 mvn/gradle; 前端: 默认 npm run build</span>
          </div>
          <div class="form-row" data-java-only style="display:none">
            <label>Java 版本</label>
            <input name="java_version" placeholder="17">
            <span class="form-hint">Maven/Gradle 镜像的 Java 版本，留空默认 17</span>
          </div>
          <div class="form-row form-full" data-frontend-only style="display:none">
            <label>构建命令</label>
            <input name="build_command" placeholder="npm run build">
            <span class="form-hint">留空使用默认：npm run build</span>
          </div>
          <div class="form-row" data-frontend-only style="display:none">
            <label>前端代码子目录</label>
            <input name="app_dir" placeholder="frontend">
            <span class="form-hint">package.json 所在子目录（相对仓库根），留空表示根目录</span>
          </div>
          <div class="form-row" data-frontend-only style="display:none">
            <label>产物目录</label>
            <input name="dist_dir" placeholder="dist">
            <span class="form-hint">构建输出目录，相对于仓库根目录，留空默认 dist</span>
          </div>
          <div class="form-row" data-frontend-only style="display:none">
            <label>Node 版本</label>
            <input name="node_version" placeholder="lts">
            <span class="form-hint">node 镜像版本，留空默认 lts</span>
          </div>
          <div class="form-row" data-frontend-only style="display:none">
            <label>nginx 容器名</label>
            <input name="nginx_container" placeholder="nginx">
            <span class="form-hint">留空则直接写宿主机目录；填写则用 docker cp 推入容器</span>
          </div>
          <div class="form-row" data-frontend-only style="display:none">
            <label>目标路径 *</label>
            <input name="deploy_target_dir" placeholder="/usr/share/nginx/html">
            <span class="form-hint">容器内路径（docker cp 模式）或宿主机挂载目录</span>
          </div>
          <div class="form-actions">
            <button type="submit" class="primary-btn">保存</button>
            <button type="button" class="cancel-btn" onclick="closeAddModal()">取消</button>
          </div>
        </div>
      </form>
    </div>
  </div>
</div>

<!-- 编辑工程弹窗 / Edit project modal -->
<div id="edit-modal" class="modal-overlay" style="display:none" onclick="if(event.target===this)closeEditModal()">
  <div class="modal-card">
    <div class="modal-h">
      <h2>编辑工程</h2>
      <button class="modal-close" onclick="closeEditModal()">×</button>
    </div>
    <div class="card-b">
      <form onsubmit="submitEditForm(event)">
        <input type="hidden" name="_id" id="edit-id">
        <div class="form-grid">
          <div class="form-row">
            <label>工程名称 *</label>
            <input id="edit-name" name="name" required>
          </div>
          <div class="form-row">
            <label>服务名 *</label>
            <input id="edit-service-name" name="service_name" required>
          </div>
          <div class="form-row form-full">
            <label>仓库地址 *</label>
            <input id="edit-repo-url" name="repo_url" required>
          </div>
          <div class="form-row">
            <label>GitHub Token</label>
            <input id="edit-github-token" name="github_token" type="password" placeholder="留空：私有项目保持原 Token，其余使用全局 Token">
          </div>
          <div class="form-row">
            <label>工程类型</label>
            <select id="edit-project-type" name="project_type" onchange="onProjectTypeChange(this)">
              <option value="">通用</option>
              <option value="python">Python</option>
              <option value="java">Java</option>
              <option value="frontend">前端</option>
            </select>
          </div>
          <div class="form-row" data-docker-only>
            <label>镜像名</label>
            <input id="edit-image-name" name="image_name" placeholder="myapp:latest">
          </div>
          <div class="form-row form-full" data-docker-only>
            <label>Compose 路径</label>
            <input id="edit-compose-file" name="compose_file" placeholder="/pi-cluster/docker-compose.yml">
          </div>
          <div class="form-row form-full" data-docker-only>
            <label class="checkbox-label">
              <input type="checkbox" id="edit-self-service" name="self_service">
              <span>自重启服务</span>
            </label>
          </div>
          <div class="form-row" data-docker-only>
            <label>自定义构建命令</label>
            <input id="edit-build-command" name="build_command" placeholder="留空使用默认">
          </div>
          <div class="form-row" data-java-only>
            <label>Java 版本</label>
            <input id="edit-java-version" name="java_version" placeholder="17">
          </div>
          <div class="form-row" data-frontend-only>
            <label>前端代码子目录</label>
            <input id="edit-app-dir" name="app_dir" placeholder="frontend">
          </div>
          <div class="form-row" data-frontend-only>
            <label>产物目录</label>
            <input id="edit-dist-dir" name="dist_dir" placeholder="dist">
          </div>
          <div class="form-row" data-frontend-only>
            <label>Node 版本</label>
            <input id="edit-node-version" name="node_version" placeholder="lts">
          </div>
          <div class="form-row" data-frontend-only>
            <label>nginx 容器名</label>
            <input id="edit-nginx-container" name="nginx_container" placeholder="nginx">
          </div>
          <div class="form-row" data-frontend-only>
            <label>目标路径</label>
            <input id="edit-deploy-target-dir" name="deploy_target_dir" placeholder="/usr/share/nginx/html">
          </div>
          <div class="form-actions">
            <button type="submit" class="primary-btn">保存</button>
            <button type="button" class="cancel-btn" onclick="closeEditModal()">取消</button>
          </div>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
{_JS}
{proj_init_js}
{auto_select_js}
</script>
</body>
</html>"""


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
