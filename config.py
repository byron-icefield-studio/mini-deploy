"""配置管理：pydantic_settings 加载 .env 并支持运行时更新。
Configuration management: load .env via pydantic_settings with runtime update support.
"""
from pathlib import Path

from dotenv import set_key
from pydantic_settings import BaseSettings, SettingsConfigDict

# 配置文件路径（容器内优先 /config/.env，本地开发回退到项目目录）
# Config file path: prefer /config/.env in container, fallback to project dir locally
ENV_FILE = Path("/config/.env")
if not ENV_FILE.parent.exists():
    ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    """Mini Deploy 全局配置。
    Mini Deploy global settings.
    """
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        validate_assignment=True,
    )

    LOG_PATH: str = "/logs"

    # 部署全局默认值（各工程可在管理页单独覆盖）
    # Global deployment defaults (each project can override in the management page)
    DEPLOY_GITHUB_TOKEN: str = ""
    DEPLOY_COMPOSE_FILE: str = "/pi-cluster/docker-compose.yml"

    def update(self, key: str, value: str) -> None:
        """运行时更新配置并持久化到 .env。
        Update config at runtime and persist to .env file.
        """
        setattr(self, key, value)
        set_key(str(ENV_FILE), key, value)


settings = Settings()
