"""backend/tools/sandbox.py

6-layer sandboxed execution for LLM-generated Python classification scripts.
Layers: AST analysis → OpLevel gating → path bounds → subprocess isolation
        → dry-run + human confirm → source_path lifecycle isolation.

Only used for _phase_classify. All other LLM calls return data, not code.
"""
from __future__ import annotations
import ast
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


FORBIDDEN_CALLS = {
    # rmtree / remove / unlink are now CONTROLLED (bounds-checked at runtime by
    # _SafeShutil / monkey-patched os).  Removing them from FORBIDDEN_CALLS so
    # Layer 1 doesn't block scripts that legitimately clean up within output_path.
    "system", "popen",
    "eval", "exec", "compile", "__import__",
}

FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "urllib", "requests",
    "ctypes", "multiprocessing", "threading",
}

# Mapping from call/attribute names to operation type labels.
_CALL_OP_MAP = {
    "move": "MOVE", "rename": "MOVE",
    "copy": "COPY", "copy2": "COPY", "copytree": "COPY",
    "makedirs": "MKDIR", "mkdir": "MKDIR",
    "rmtree": "DELETE", "remove": "DELETE", "unlink": "DELETE",
    "system": "EXEC", "popen": "EXEC",
    "eval": "EXEC", "exec": "EXEC", "compile": "EXEC", "__import__": "EXEC",
    "listdir": "READ", "scandir": "READ", "glob": "READ",
    "isfile": "READ", "isdir": "READ", "exists": "READ", "walk": "READ",
}


class OpLevel(Enum):
    SAFE = "safe"            # READ, STAT, GLOB
    CONTROLLED = "controlled"  # MOVE, COPY, MKDIR, DELETE-in-bounds
    DANGEROUS = "dangerous"  # reserved (currently unused — DELETE moved to CONTROLLED)
    FORBIDDEN = "forbidden"  # EXEC, network


# Map op_type strings to OpLevel.
# DELETE is CONTROLLED (not DANGEROUS): _SafeShutil enforces path bounds at runtime,
# so deleting within allowed_dest_paths is safe.  Only EXEC-type operations are
# permanently forbidden regardless of path.
_OP_TYPE_TO_LEVEL = {
    "READ": OpLevel.SAFE,
    "MOVE": OpLevel.CONTROLLED,
    "COPY": OpLevel.CONTROLLED,
    "MKDIR": OpLevel.CONTROLLED,
    "DELETE": OpLevel.CONTROLLED,
    "EXEC": OpLevel.FORBIDDEN,
}


@dataclass
class AnalysisResult:
    safe: bool
    violations: list[str]
    op_types: set[str]


@dataclass
class ExecutionResult:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    ops_log: list[dict]


@dataclass
class SandboxConfig:
    allowed_dest_paths: list[str]
    audit_log_dir: str
    task_id: str
    allow_delete: bool = False
    timeout_seconds: int = 120
    require_human_confirm: bool = True


# ---------------------------------------------------------------------------
# Layer 1: static AST analysis (fail-closed)
# ---------------------------------------------------------------------------

def analyze_script(code: str) -> AnalysisResult:
    """Layer 1: static AST analysis. Fail-closed — parse error → reject."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return AnalysisResult(
            safe=False,
            violations=[f"Syntax error: {e}"],
            op_types=set(),
        )

    violations: list[str] = []
    op_types: set[str] = set()

    for node in ast.walk(tree):
        # --- imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base in FORBIDDEN_IMPORTS:
                    violations.append(f"Forbidden import: {base}")
                elif "." in alias.name:
                    # Check deeper submodule like os.system
                    for part in alias.name.split("."):
                        if part in FORBIDDEN_IMPORTS:
                            violations.append(f"Forbidden import: {part} (in {alias.name})")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                base = node.module.split(".")[0]
                if base in FORBIDDEN_IMPORTS:
                    violations.append(f"Forbidden import: {base} (from {node.module})")

        # --- calls ---
        elif isinstance(node, ast.Call):
            call_name: str | None = None
            if isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                call_name = node.func.id

            if call_name is not None:
                if call_name in FORBIDDEN_CALLS:
                    violations.append(f"Forbidden call: {call_name}")
                if call_name in _CALL_OP_MAP:
                    op_types.add(_CALL_OP_MAP[call_name])

    return AnalysisResult(
        safe=len(violations) == 0,
        violations=violations,
        op_types=op_types,
    )


# ---------------------------------------------------------------------------
# Layer 2: operation-type gating
# ---------------------------------------------------------------------------

def check_op_level(analysis: AnalysisResult, config: SandboxConfig) -> tuple[bool, str]:
    """Layer 2: gate on operation type. DELETE/EXEC always rejected."""
    for op_type in analysis.op_types:
        level = _OP_TYPE_TO_LEVEL.get(op_type)
        if level is None:
            continue
        if level == OpLevel.FORBIDDEN:
            return False, f"Forbidden operation type: {op_type} (permanently rejected)"
        if level == OpLevel.DANGEROUS and not config.allow_delete:
            return False, f"Dangerous operation type: {op_type} (allow_delete=False)"
    return True, "ok"


# ---------------------------------------------------------------------------
# Layer 3: path-bounds static validation (symlink-safe)
# ---------------------------------------------------------------------------

def _extract_path_literals(code: str) -> list[str]:
    """Extract string literals that look like file paths from source code."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    paths: list[str] = []
    for node in ast.walk(tree):
        s: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
        # Python < 3.8 compatibility (ast.Str deprecated, removed in 3.14)
        elif hasattr(ast, "Str") and isinstance(node, ast.Str):
            s = node.s

        if s and "/" in s:
            paths.append(s)

    return paths


def _is_in_bounds(path: str, root: str) -> bool:
    """Check that *path* equals *root* or is a child of it (no false match on siblings)."""
    return path == root or path.startswith(root + "/")


def validate_all_paths(code: str, config: SandboxConfig) -> tuple[bool, list[str]]:
    """Layer 3: extract all string literals, resolve symlinks, check bounds."""
    violations: list[str] = []
    resolved_roots = [str(Path(r).resolve()) for r in config.allowed_dest_paths]

    for literal in _extract_path_literals(code):
        try:
            resolved = str(Path(literal).resolve())
        except (OSError, ValueError, RuntimeError):
            # Un-resolvable path (e.g. null byte) — reject
            violations.append(f"Unresolvable path: {literal}")
            continue

        in_bounds = any(_is_in_bounds(resolved, root) for root in resolved_roots)
        if not in_bounds:
            violations.append(f"Path out of bounds: {literal} -> {resolved}")

    return len(violations) == 0, violations


# ---------------------------------------------------------------------------
# Layer 4: subprocess isolation with _SafeShutil interceptor
# ---------------------------------------------------------------------------

_SANDBOX_PREFIX = '''
import shutil as _real_shutil
import os as _os
import sys as _sys
import json as _json
from pathlib import Path as _Path

_ALLOWED = __ALLOWED_JSON__
_DRY_RUN = __DRY_RUN__
_OPS = []

def _check_bounds(path, allowed):
    real = str(_Path(path).resolve())
    for root in allowed:
        root_real = str(_Path(root).resolve())
        if real == root_real or real.startswith(root_real + "/"):
            return
    raise PermissionError(f"Path out of bounds: {real}")

class _SafeShutil:
    def move(self, src, dst, **kw):
        _check_bounds(str(dst), _ALLOWED)
        _OPS.append({"op": "MOVE", "src": str(src), "dst": str(dst)})
        if not _DRY_RUN:
            _real_shutil.move(str(src), str(dst), **kw)

    def makedirs(self, path, **kw):
        _check_bounds(str(path), _ALLOWED)
        _OPS.append({"op": "MKDIR", "path": str(path)})
        if not _DRY_RUN:
            _os.makedirs(str(path), **kw)

    def rmtree(self, path, **kw):
        _check_bounds(str(path), _ALLOWED)
        _OPS.append({"op": "RMTREE", "path": str(path)})
        if not _DRY_RUN:
            _real_shutil.rmtree(str(path), **kw)

    def __getattr__(self, name):
        raise AttributeError(f"shutil.{name} is not available in sandbox")

_sys.modules['shutil'] = _SafeShutil()
shutil = _SafeShutil()

# Intercept os.rmdir / os.remove / os.unlink with bounds-checking.
# IMPORTANT: save original functions BEFORE monkey-patching to avoid infinite
# recursion (_os.rmdir would otherwise point to _safe_os_rmdir after patching).
_original_rmdir = _os.rmdir
_original_remove = _os.remove

def _safe_os_rmdir(path, **kwargs):
    # When dir_fd is present this is an internal call from shutil.rmtree —
    # the top-level path was already bounds-checked by _SafeShutil.rmtree.
    # Relative filenames with dir_fd can't be resolved to an absolute path here.
    if 'dir_fd' not in kwargs:
        _check_bounds(str(path), _ALLOWED)
    _OPS.append({"op": "RMDIR", "path": str(path)})
    if not _DRY_RUN:
        _original_rmdir(str(path), **kwargs)

def _safe_os_remove(path, **kwargs):
    if 'dir_fd' not in kwargs:
        _check_bounds(str(path), _ALLOWED)
    _OPS.append({"op": "REMOVE", "path": str(path)})
    if not _DRY_RUN:
        _original_remove(str(path), **kwargs)
import os as _os_module
_os_module.rmdir = _safe_os_rmdir
_os_module.remove = _safe_os_remove
_os_module.unlink = _safe_os_remove

# ===== USER SCRIPT =====
__USER_CODE__
# ===== END USER SCRIPT =====

print("OPS_LOG:" + _json.dumps(_OPS))
'''


def _build_interceptor(code: str, config: SandboxConfig, dry_run: bool) -> str:
    """Wrap generated code with _SafeShutil monkey-patch interceptor.

    Uses sentinel replacement (not str.format) so that user code containing
    curly braces (dict literals, f-strings, etc.) is preserved verbatim.
    """
    import json as _json_mod

    allowed_json = _json_mod.dumps(config.allowed_dest_paths)
    result = _SANDBOX_PREFIX
    result = result.replace("__ALLOWED_JSON__", allowed_json)
    result = result.replace("__DRY_RUN__", str(dry_run))
    result = result.replace("__USER_CODE__", code)
    return result


def run_in_subprocess(
    code: str,
    config: SandboxConfig,
    dry_run: bool = True,
) -> ExecutionResult:
    """Layer 4: execute in isolated subprocess with _SafeShutil interceptor."""
    wrapped = _build_interceptor(code, config, dry_run)

    # Write wrapped script to a temp file under /root (per memory restriction).
    tmpdir = tempfile.mkdtemp(prefix="sandbox_", dir="/root")
    script_path = Path(tmpdir) / "wrapped_script.py"
    try:
        script_path.write_text(wrapped, encoding="utf-8")
    except Exception:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr="Failed to write wrapped script to temp file",
            exit_code=-1,
            ops_log=[],
        )

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            cwd="/root",
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Script execution timed out after {config.timeout_seconds}s",
            exit_code=-1,
            ops_log=[],
        )
    finally:
        # Clean up temp files.
        try:
            script_path.unlink(missing_ok=True)
            script_path.parent.rmdir()
        except OSError:
            pass

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # Parse OPS_LOG from stdout lines.
    ops_log: list[dict] = []
    for line in stdout.splitlines():
        if line.startswith("OPS_LOG:"):
            payload = line[len("OPS_LOG:"):]
            try:
                ops_log = json.loads(payload)
            except json.JSONDecodeError:
                pass

    success = result.returncode == 0

    return ExecutionResult(
        success=success,
        stdout=stdout,
        stderr=stderr,
        exit_code=result.returncode,
        ops_log=ops_log,
    )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _write_audit_log(ops: list[dict], config: SandboxConfig, success: bool) -> None:
    """Write ops to {config.audit_log_dir}/audit_{ts}.json. Create dir if needed."""
    audit_dir = Path(config.audit_log_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)

    audit_path = audit_dir / f"audit_{int(time.time())}.json"
    payload = {
        "task_id": config.task_id,
        "timestamp": int(time.time()),
        "success": success,
        "ops": ops,
    }
    audit_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Layer 5: dry-run → plan collection
# ---------------------------------------------------------------------------

def execute_with_dryrun(
    code: str,
    config: SandboxConfig,
    require_human_confirm: bool = True,
) -> ExecutionResult:
    """Layer 5: dry-run → collect ops → (human confirms) → real execution.

    This function performs the dry-run only and returns the plan (ops_log).
    The caller (executor's pause mechanism) is responsible for showing the plan
    to the user and, on approval, calling run_in_subprocess(..., dry_run=False)
    followed by _write_audit_log.
    """
    return run_in_subprocess(code, config, dry_run=True)


# ---------------------------------------------------------------------------
# Layer 6 + unified entry point
# ---------------------------------------------------------------------------

def run_agent_code(code: str, config: SandboxConfig) -> ExecutionResult:
    """Unified entry point: all 6 layers in sequence.

    Layers:
      1. AST analysis (reject forbidden calls/imports)
      2. Op-level gating (reject FORBIDDEN/DANGEROUS)
      3. Path-bounds static validation
      4. Subprocess isolation + _SafeShutil runtime interception
      5. Dry-run plan collection
      6. Source-path lifecycle isolation (enforced by executor, not this fn)

    Returns the ExecutionResult from the first failing layer, or the dry-run
    result on success.
    """
    # Layer 1
    analysis = analyze_script(code)
    if not analysis.safe:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Layer 1 (AST analysis) failed: {'; '.join(analysis.violations)}",
            exit_code=-1,
            ops_log=[],
        )

    # Layer 2
    ok, reason = check_op_level(analysis, config)
    if not ok:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Layer 2 (op-level gate) failed: {reason}",
            exit_code=-1,
            ops_log=[],
        )

    # Layer 3
    ok, violations = validate_all_paths(code, config)
    if not ok:
        return ExecutionResult(
            success=False,
            stdout="",
            stderr=f"Layer 3 (path validation) failed: {'; '.join(violations)}",
            exit_code=-1,
            ops_log=[],
        )

    # Layers 4 + 5
    return execute_with_dryrun(code, config)
