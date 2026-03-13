"""运行时状态，部署任务更新此处，页面从此处读取。
Runtime state: deploy tasks update here, pages read from here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ProjectDeployState:
    """单个工程的部署状态。
    Deployment state for a single project.
    """
    phase: str = "idle"          # idle / pulling / building / restarting / done / error
    step: str = "idle"           # 当前流程步骤，用于进度条展示 / current pipeline step for progress bar
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    logs: list = field(default_factory=list)
    error: Optional[str] = None
    history: list = field(default_factory=list)  # 最近5次 {time, duration, success}


@dataclass
class DeployState:
    """全部工程的部署状态容器。
    Container for all projects' deployment states.
    """
    projects: dict = field(default_factory=dict)  # project_id -> ProjectDeployState

    def get_project(self, project_id: str) -> ProjectDeployState:
        """获取或创建工程部署状态。
        Get or create project deploy state.
        """
        if project_id not in self.projects:
            self.projects[project_id] = ProjectDeployState()
        return self.projects[project_id]

    def active_count(self) -> int:
        """当前正在执行部署的工程数。
        Count of projects currently deploying.
        """
        return sum(
            1 for ps in self.projects.values()
            if ps.phase in ("pulling", "building", "restarting")
        )


deploy = DeployState()
