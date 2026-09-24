"""Worker：轮询 SQLite 任务队列，线程池并发执行。

纯 API 任务（tts+api模型, prompt_experiment_v3, kb_aggregation, dataset_ingest）可并发。
sglang/direct_python 类型任务需独占 GPU，串行执行。
"""
import time
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.database import SessionLocal, init_db
from backend.models.task import Task, ModelRegistry
from backend.worker.init_models import init_models
from backend.pipeline.dataset import run_dataset_pipeline

POLL_INTERVAL = 2
PID_FILE = Path("/tmp/ai_workflow_worker.pid")

MAX_CONCURRENT = 3  # 最多同时运行的任务数


def _acquire_pid_lock() -> bool:
    """返回 True 表示拿到锁（当前是唯一实例），False 表示已有其他 worker 在运行。"""
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text().strip())
            os.kill(existing_pid, 0)  # 进程存在才继续检查
            # 确认是 worker 进程，而非 PID 被其他程序复用
            cmdline = Path(f"/proc/{existing_pid}/cmdline").read_text().replace("\x00", " ")
            if "worker" in cmdline:
                print(f"[Worker] 已有 worker 运行中 (pid={existing_pid})，退出")
                return False
        except (ProcessLookupError, ValueError, FileNotFoundError, OSError):
            pass
    PID_FILE.write_text(str(os.getpid()))
    return True


def _release_pid_lock():
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


AGENT_TASK_TYPES = {"prompt_experiment", "prompt_experiment_v3", "prompt_experiment_v3_agent",
                    "kb_aggregation",
                    "data_process", "model_evaluate", "model_install"}


def _is_agent_loop_task(task: Task) -> bool:
    """判断 v3 任务是否走新 agent loop executor"""
    if task.type == "prompt_experiment_v3_agent":
        return True
    if task.type == "prompt_experiment_v3":
        try:
            input_data = json.loads(task.input or "{}")
            return "agent_events" in input_data or input_data.get("phase") == "start"
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def _is_api_only_task(task: Task, db) -> bool:
    """判断任务是否仅使用 API 模型，可安全并发。"""
    # Agent 类任务：调 LLM API + TTS API，天然可并发
    if task.type in AGENT_TASK_TYPES:
        return True
    if task.type == "dataset_ingest":
        return True
    if task.type == "dataset":
        return True
    if task.type == "tts":
        try:
            input_data = json.loads(task.input or "{}")
            model_id = input_data.get("model_id", "")
            model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
            return model and model.model_type == "api"
        except (json.JSONDecodeError, TypeError):
            return False
    return False


def _requires_gpu(task: Task, db) -> bool:
    """判断任务是否需要独占 GPU（sglang / direct_python）。"""
    if task.type == "tts":
        try:
            input_data = json.loads(task.input or "{}")
            model_id = input_data.get("model_id", "")
            model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
            return model and model.model_type in ("sglang", "direct_python")
        except (json.JSONDecodeError, TypeError):
            return False
    return False


def _dispatch(task: Task, db):
    if task.type == "dataset":
        return run_dataset_pipeline(task, db)

    if task.type == "dataset_ingest":
        from backend.executors.dataset_ingest_executor import DatasetIngestExecutor
        DatasetIngestExecutor().execute(task, db)
        return None  # status managed by executor (_pause sets awaiting_review, done sets success)

    if task.type in AGENT_TASK_TYPES:
        if _is_agent_loop_task(task):
            import backend.executors.agent_loop_executor  # noqa
            from backend.executors.agent_loop_executor import AgentLoopExecutor
            executor = AgentLoopExecutor()
            executor.execute(task, db)
            return None
        if task.type == "prompt_experiment":
            import backend.executors.prompt_experiment  # noqa
        elif task.type == "prompt_experiment_v3":
            import backend.executors.prompt_experiment_v3  # noqa
        elif task.type == "kb_aggregation":
            import backend.executors.kb_aggregation  # noqa
        from backend.executors.agent_base import AgentExecutorRegistry
        executor = AgentExecutorRegistry.get(task.type)
        executor.execute(task, db)
        return None  # status 由 executor 自己管理

    if task.type == "tts":
        import json as _json
        from backend.executors.base import ExecutorFactory
        # 延迟导入，触发 executor 注册
        import backend.executors.subprocess_exec  # noqa: F401
        import backend.executors.sglang_exec      # noqa: F401
        import backend.executors.api_exec         # noqa: F401

        input_data = _json.loads(task.input or "{}")
        model_id = input_data.get("model_id", "fish-audio")
        model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
        if model is None or model.status != "active":
            raise ValueError(f"模型 {model_id} 不可用（status={getattr(model, 'status', None)}）")

        executor = ExecutorFactory.get(model.model_type)
        result = executor.execute(task, model, db)

        # sglang 任务结束后立即释放 GPU 显存，避免后续 subprocess 模型 OOM
        if model.model_type == "sglang":
            try:
                from backend.executors.sglang_manager import ServiceManager
                ServiceManager().stop()
            except Exception:
                pass

        # BenchmarkRun 子任务完成后更新父状态
        benchmark_run_id = input_data.get("benchmark_run_id")
        if benchmark_run_id:
            _update_benchmark_run(benchmark_run_id, db)

        return result.to_dict()

    raise ValueError(f"Unknown task type: {task.type}")


def _update_benchmark_run(run_id: str, db) -> None:
    from backend.models.task import BenchmarkRun
    run = db.get(BenchmarkRun, run_id)
    if run is None:
        return
    tasks = db.query(Task).filter(Task.input.contains(run_id)).all()
    statuses = {t.status for t in tasks}
    if "pending" in statuses or "running" in statuses:
        run.status = "running"
    elif "failed" in statuses:
        run.status = "partial"
    else:
        run.status = "done"
        run.finished_at = datetime.utcnow()
    db.commit()


def _check_stale_tasks(db) -> int:
    """检查僵死任务（>10min 无更新），强制标记失败。返回僵死任务数。"""
    stale_count = 0
    running_tasks = db.query(Task).filter(Task.status == "running").all()
    for rt in running_tasks:
        if rt.updated_at and (datetime.utcnow() - rt.updated_at).total_seconds() > 600:
            rt.status = "failed"
            rt.error = "任务超时（>10min 无更新），自动标记失败"
            rt.finished_at = datetime.utcnow()
            stale_count += 1
            print(f"[Worker] stale task {rt.id[:8]} force-failed")
    if stale_count:
        db.commit()
    return stale_count


def _run_task_in_thread(task_id: str):
    """在线程池中执行任务。每个线程拥有独立的 db session。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if task is None:
            print(f"[Worker] task {task_id} not found (skipped)")
            return

        try:
            result = _dispatch(task, db)
            # 执行器可能在内部设了 awaiting_review（agent 任务断点续跑）
            if task.status == "awaiting_review":
                pass
            elif isinstance(result, dict):
                task.result = json.dumps(result, ensure_ascii=False)
                ok = result.get("ok", result.get("success"))
                fail = result.get("fail", result.get("failed"))
                if ok is not None and fail is not None and ok == 0 and fail > 0:
                    task.status = "failed"
                    task.error = f"所有条目失败（{fail} 条）"
                else:
                    task.status = "success"
            else:
                # agent executor 正常返回 None → success
                task.status = "success"
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            import time as _t, json as _j, shutil as _sh
            from pathlib import Path as _P
            err_msg = _j.dumps({
                "type": "error",
                "ts": int(_t.time() * 1000),
                "message": f"执行失败: {e}",
            }, ensure_ascii=False)
            task.append_log(err_msg, "error")
            print(f"[Worker] task {task.id} FAILED: {e}")
            # Clean up preview_cache on failure
            try:
                from backend.config import STORAGE_DIR
                preview_cache = _P(STORAGE_DIR) / "ingest_sessions" / task.id / "preview_cache"
                if preview_cache.exists():
                    _sh.rmtree(str(preview_cache), ignore_errors=True)
            except Exception:
                pass

        # awaiting_review 是中间状态，不设 finished_at
        if task.status != "awaiting_review":
            task.finished_at = datetime.utcnow()
        task.updated_at = datetime.utcnow()
        db.commit()
        print(f"[Worker] task {task.id} -> {task.status}")
    except Exception as e:
        print(f"[Worker] _run_task_in_thread error: {e}")
    finally:
        db.close()


def run():
    if not _acquire_pid_lock():
        sys.exit(1)

    try:
        init_db()
        db = SessionLocal()
        try:
            init_models(db)
        finally:
            db.close()

        print(f"[Worker] started (pid={os.getpid()}), max_concurrent={MAX_CONCURRENT}, polling every {POLL_INTERVAL}s")

        pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="worker")
        active_futures: dict[str, Future] = {}  # task_id → Future
        active_gpu_task_id: str | None = None   # 当前独占 GPU 的任务 ID
        lock = threading.Lock()

        while True:
            db = SessionLocal()
            try:
                # 1. 僵死检测
                _check_stale_tasks(db)

                # 2. 清理已完成的 future
                with lock:
                    done_ids = [tid for tid, fut in active_futures.items() if fut.done()]
                    for tid in done_ids:
                        active_futures.pop(tid)
                        if tid == active_gpu_task_id:
                            active_gpu_task_id = None

                # 3. 取 pending 任务，逐个判断是否可以启动
                pending = (
                    db.query(Task)
                    .filter(Task.status == "pending")
                    .order_by(Task.created_at)
                    .all()
                )

                for task in pending:
                    with lock:
                        n_active = len(active_futures)

                    # 并发度上限
                    if n_active >= MAX_CONCURRENT:
                        break

                    # GPU 独占检查：有 GPU 任务在跑时，新任务必须等
                    is_gpu = _requires_gpu(task, db)
                    with lock:
                        gpu_busy = active_gpu_task_id is not None

                    if is_gpu and n_active > 0:
                        # GPU 任务必须独占，等所有其他任务完成
                        continue
                    if gpu_busy and not is_gpu:
                        # GPU 正忙，非 GPU 任务可以并发（API 任务不受 GPU 影响）
                        # 但如果是另一个 GPU 任务，必须等
                        pass
                    if gpu_busy and is_gpu:
                        # GPU 正忙，新 GPU 任务必须等
                        continue

                    # 原子领取：将 pending → running（防止并发竞争同一任务）
                    stmt = (
                        __import__('sqlalchemy').update(Task)
                        .where(Task.id == task.id, Task.status == "pending")
                        .values(status="running", updated_at=datetime.utcnow())
                    )
                    result = db.execute(stmt)
                    db.commit()
                    if result.rowcount == 0:
                        continue  # 被其他线程抢先了，跳过

                    print(f"[Worker] picked up task {task.id} type={task.type} (concurrent={n_active + 1})")

                    with lock:
                        if is_gpu:
                            active_gpu_task_id = task.id
                        active_futures[task.id] = pool.submit(_run_task_in_thread, task.id)

            except Exception as e:
                print(f"[Worker] loop error: {e}")
            finally:
                db.close()
            time.sleep(POLL_INTERVAL)
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    run()
