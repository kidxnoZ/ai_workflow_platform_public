"""AgentBaseExecutor：多阶段 Agent 任务的基类。

与 TTS BaseExecutor 的区别：
- execute(task, db) 无 model 参数（agent 任务无对应 ModelRegistry 记录）
- 不返回 ExecutorResult；状态由执行器自己管理
- 支持 awaiting_review 断点：调用 _pause() 后返回，Worker 不覆盖状态
- phase dispatch：读 task.input["phase"]，调用对应的 _phase_{name}() 方法
"""
import json
from abc import ABC, abstractmethod
from datetime import datetime
from sqlalchemy.orm import Session


class AgentBaseExecutor(ABC):
    def execute(self, task, db: Session) -> None:
        """
        入口。读当前 phase，dispatch 到对应方法。
        子类通过实现 _phase_{name}(task, input_data, db) 来处理各阶段。
        """
        input_data = json.loads(task.input or "{}")
        phase = input_data.get("phase", "start")
        handler = getattr(self, f"_phase_{phase}", None)
        if handler is None:
            raise ValueError(
                f"{self.__class__.__name__}: unknown phase {phase!r}. "
                f"Implement _phase_{phase}() to handle it."
            )
        # 记录阶段开始时间
        timings: dict = input_data.get("phase_timings", {})
        timings.setdefault(phase, {})["began_at"] = datetime.utcnow().isoformat() + "Z"
        input_data["phase_timings"] = timings
        task.input = json.dumps(input_data, ensure_ascii=False)
        db.commit()

        handler(task, input_data, db)

        # 最后一个阶段（不调 _pause）：在这里补记 ended_at
        if task.status != "awaiting_review":
            timings = input_data.get("phase_timings", {})
            timings.setdefault(phase, {})["ended_at"] = datetime.utcnow().isoformat() + "Z"
            input_data["phase_timings"] = timings
            task.input = json.dumps(input_data, ensure_ascii=False)
            db.commit()

    def _pause(self, task, input_data: dict, next_phase: str, db: Session) -> None:
        """暂停任务，等待人工操作。

        将 phase 设为 next_phase，status 设为 awaiting_review，commit 后返回。
        Worker 看到 awaiting_review 会跳过状态覆盖，保持暂停状态。
        人工完成操作后，前端调 review API 把 status 改回 pending，任务重入队，
        下次 Worker 捞到时从 next_phase 继续。
        """
        # 记录当前阶段结束时间
        current_phase = input_data.get("phase", "start")
        timings: dict = input_data.get("phase_timings", {})
        timings.setdefault(current_phase, {})["ended_at"] = datetime.utcnow().isoformat() + "Z"
        input_data["phase_timings"] = timings

        input_data["phase"] = next_phase
        task.input = json.dumps(input_data, ensure_ascii=False)
        task.status = "awaiting_review"
        task.updated_at = datetime.utcnow()
        db.commit()

    def _save_phase_data(self, task, input_data: dict, db: Session) -> None:
        """仅更新 task.input（不改 status），用于阶段内持久化中间数据。"""
        task.input = json.dumps(input_data, ensure_ascii=False)
        task.updated_at = datetime.utcnow()
        db.commit()


class AgentExecutorRegistry:
    _registry: dict[str, type[AgentBaseExecutor]] = {}

    @classmethod
    def register(cls, task_type: str, executor_cls: type[AgentBaseExecutor]) -> None:
        cls._registry[task_type] = executor_cls

    @classmethod
    def get(cls, task_type: str) -> AgentBaseExecutor:
        executor_cls = cls._registry.get(task_type)
        if executor_cls is None:
            raise ValueError(
                f"No agent executor registered for task_type={task_type!r}. "
                f"Registered: {list(cls._registry)}"
            )
        return executor_cls()
