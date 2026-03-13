"""Mini Deploy 入口：轻量级部署管理服务。
Mini Deploy entry point: lightweight deployment management service.

支持 Python / Java / Frontend / Generic Docker 项目的一键部署。
Supports one-click deployment for Python / Java / Frontend / Generic Docker projects.
"""
from fastapi import FastAPI
from deploy_api import deploy_router

app = FastAPI(title="Mini Deploy")
app.include_router(deploy_router)
