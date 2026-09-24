from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from sqlalchemy.orm import Session


@dataclass
class ExecutorResult:
    items: list[dict] = field(default_factory=list)
    total: int = 0
    ok: int = 0
    fail: int = 0

    def to_dict(self) -> dict:
        return {"items": self.items, "total": self.total, "ok": self.ok, "fail": self.fail}


class BaseExecutor(ABC):
    @abstractmethod
    def execute(self, task, model, db: Session) -> ExecutorResult:
        ...


class ExecutorFactory:
    _registry: dict[str, type[BaseExecutor]] = {}

    @classmethod
    def register(cls, model_type: str, executor_cls: type[BaseExecutor]) -> None:
        cls._registry[model_type] = executor_cls

    @classmethod
    def get(cls, model_type: str) -> BaseExecutor:
        executor_cls = cls._registry.get(model_type)
        if executor_cls is None:
            raise ValueError(f"No executor registered for model_type={model_type!r}")
        return executor_cls()
