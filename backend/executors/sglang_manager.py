"""ServiceManager：管理 sglang-omni 服务进程（单例，port 8001）。"""
import json
import subprocess
import time
import threading
from typing import Optional

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

SGLANG_OMNI_DIR = "/root/workspace/sglang-omni"
SYSTEM_PYTHON = "/usr/bin/python3"
SERVICE_PORT = 8001
HEALTH_URL = f"http://localhost:{SERVICE_PORT}/health"
STARTUP_TIMEOUT = 600  # 秒，等待服务就绪（大模型冷启动慢）
ALIYUN_MIRROR = "https://mirrors.aliyun.com/pypi/simple"


def _ensure_sglang_omni_installed() -> None:
    check = subprocess.run(
        [SYSTEM_PYTHON, "-c", "import sglang_omni"],
        capture_output=True,
    )
    if check.returncode != 0:
        subprocess.run(
            [SYSTEM_PYTHON, "-m", "pip", "install", "--break-system-packages",
             "-e", SGLANG_OMNI_DIR, "--no-deps", "-q"],
            check=True,
        )
        subprocess.run(
            [SYSTEM_PYTHON, "-m", "pip", "install", "--break-system-packages",
             "msgpack", "-q",
             "-i", ALIYUN_MIRROR, "--trusted-host", "mirrors.aliyun.com"],
            check=True,
        )


class ServiceManager:
    _instance: Optional["ServiceManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
        return cls._instance

    def _init(self):
        self.current_model_id: Optional[str] = None
        self.current_process: Optional[subprocess.Popen] = None
        # 清理上一个 worker 实例留下的孤儿 sglang
        self._kill_port_owner()

    def ensure_running(self, model) -> None:
        """确保对应模型的 sglang-omni 服务正在运行且健康。"""
        extra = json.loads(model.extra_config or "{}")
        model_path = extra.get("model_path", "")

        if self.current_model_id == model.id and self._is_healthy():
            return

        self._stop()
        _ensure_sglang_omni_installed()
        self._start(model, model_path, extra.get("yaml_config"))
        if not self._wait_healthy():
            self._stop()  # 超时时主动 kill，释放 GPU 显存
            raise RuntimeError(f"sglang-omni 服务启动超时（{STARTUP_TIMEOUT}s），模型: {model.id}")

        self.current_model_id = model.id

    def _start(self, model, model_path: str, yaml_config: Optional[str]) -> None:
        cmd = [
            SYSTEM_PYTHON, "-m", "sglang_omni.cli", "serve",
            "--model-path", model_path,
            "--port", str(SERVICE_PORT),
        ]
        if yaml_config:
            cmd += ["--config", yaml_config]

        self.current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # 新 session，方便整组 kill
        )

    def _stop(self) -> None:
        import os, signal
        if self.current_process is not None:
            try:
                pgid = self.current_process.pid
                os.killpg(pgid, signal.SIGTERM)
                self.current_process.wait(timeout=30)
            except Exception:
                try:
                    os.killpg(self.current_process.pid, signal.SIGKILL)
                except Exception:
                    try:
                        self.current_process.kill()
                    except Exception:
                        pass
            self.current_process = None
        self._kill_port_owner()
        self.current_model_id = None
        self._wait_gpu_memory_free(timeout=60)

    def _kill_port_owner(self) -> None:
        """Kill any process (and its descendants) still listening on SERVICE_PORT."""
        import os, signal
        try:
            result = subprocess.run(
                ["fuser", f"{SERVICE_PORT}/tcp"],
                capture_output=True, text=True,
            )
            for pid_str in result.stdout.split():
                try:
                    pid = int(pid_str.strip())
                    # 先尝试 killpg（如果它是自己的组 leader）
                    try:
                        if os.getpgid(pid) == pid:
                            os.killpg(pid, signal.SIGKILL)
                            continue
                    except Exception:
                        pass
                    # 不是组 leader 就直接 kill 进程树
                    self._kill_tree(pid)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _kill_tree(pid: int) -> None:
        """递归 SIGKILL 进程及其所有子进程。"""
        import os, signal
        try:
            children_result = subprocess.run(
                ["ps", "--ppid", str(pid), "-o", "pid", "--no-headers"],
                capture_output=True, text=True,
            )
            for child_str in children_result.stdout.split():
                try:
                    ServiceManager._kill_tree(int(child_str.strip()))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    def _wait_gpu_memory_free(self, timeout: int = 60) -> None:
        """Wait for GPU memory to actually be released after process kill."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                used_mb = int(result.stdout.strip().split('\n')[0])
                if used_mb < 2000:
                    return
            except Exception:
                pass
            time.sleep(2)
        # Timeout — try nvidia-smi reset as last resort
        try:
            subprocess.run(
                ["nvidia-smi", "--gpu-reset"],
                capture_output=True, timeout=10,
            )
            time.sleep(3)
        except Exception:
            pass

    def _is_healthy(self) -> bool:
        if not _REQUESTS_OK or self.current_process is None:
            return False
        try:
            r = requests.get(HEALTH_URL, timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _wait_healthy(self) -> bool:
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._is_healthy():
                return True
            time.sleep(2)
        return False

    def stop(self) -> None:
        self._stop()
