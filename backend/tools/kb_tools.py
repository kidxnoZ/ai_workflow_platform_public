"""KB 工具：知识库读写、检索、覆盖矩阵操作。

所有路径相对于 KB_ROOT。Agent 主循环通过这些工具与知识库交互。
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from datetime import date

from backend.config import STORAGE_DIR

KB_ROOT = STORAGE_DIR / "kb"

# 并发写保护：KB 文件写入在同一进程内串行化，防止并发任务的读-改-写和追加操作互相损坏
_kb_write_lock = threading.Lock()


def _ensure_kb():
    KB_ROOT.mkdir(parents=True, exist_ok=True)


def grep_kb(query: str, directories: list[str] | None = None) -> list[dict]:
    """
    在 kb/ 目录下递归 grep，返回匹配文件列表及上下文。
    directories: 限定搜索目录，如 ["experiences", "factors"]
    返回: [{"file": "experiences/游戏剧情/xxx.md", "snippet": "...匹配行及上下文..."}]
    """
    _ensure_kb()
    search_paths = []
    if directories:
        for d in directories:
            p = KB_ROOT / d
            if p.exists():
                search_paths.append(str(p))
    if not search_paths:
        search_paths = [str(KB_ROOT)]

    results = []
    for sp in search_paths:
        try:
            out = subprocess.run(
                ["grep", "-r", "-l", "-i", query, sp],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                for fpath in out.stdout.strip().split("\n"):
                    if not fpath:
                        continue
                    snippet_out = subprocess.run(
                        ["grep", "-i", "-B1", "-A2", query, fpath],
                        capture_output=True, text=True, timeout=5,
                    )
                    rel = os.path.relpath(fpath, KB_ROOT)
                    results.append({
                        "file": rel,
                        "snippet": snippet_out.stdout[:500],
                    })
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return results


def glob_kb(pattern: str, directory: str | None = None) -> list[str]:
    """
    按路径模式匹配 kb/ 下的文件。
    pattern: glob 模式，如 "experiences/游戏剧情/*.md"
    返回: 匹配的文件相对路径列表
    """
    _ensure_kb()
    base = KB_ROOT / directory if directory else KB_ROOT
    if not base.exists():
        return []
    matches = sorted(base.glob(pattern))
    return [str(m.relative_to(KB_ROOT)) for m in matches if m.is_file()]


def read_kb_file(path: str) -> str:
    """读取 kb/ 下指定文件全文。path 相对于 kb/ 目录。"""
    _ensure_kb()
    full = KB_ROOT / path
    if not full.exists():
        return ""
    return full.read_text(encoding="utf-8")


def check_coverage(scene_type: str, candidate_model_ids: list[str]) -> dict:
    """
    读取覆盖矩阵，返回数据盲区。
    返回: {
        "gaps": [{"model_id": "fish-audio", "scene_type": "游戏剧情", "data_points": 0}],
        "summary": "fish-audio 在游戏剧情场景无历史数据"
    }
    """
    matrix_path = KB_ROOT / "coverage" / "model_scene_matrix.md"
    if not matrix_path.exists():
        gaps = [{"model_id": mid, "scene_type": scene_type, "data_points": 0}
                for mid in candidate_model_ids]
        return {
            "gaps": gaps,
            "summary": f"覆盖矩阵文件不存在，所有模型在 {scene_type} 场景均无历史数据（冷启动）",
        }

    content = matrix_path.read_text(encoding="utf-8")
    gaps = []
    for model_id in candidate_model_ids:
        pattern = rf"\|\s*{re.escape(model_id)}\s*\|"
        row_match = re.search(pattern, content)
        if not row_match:
            gaps.append({"model_id": model_id, "scene_type": scene_type, "data_points": 0})
            continue
        row_line = content[row_match.start():content.find("\n", row_match.start())]
        if not scene_type or scene_type not in row_line or (scene_type and "0" in row_line.split(scene_type)[-1][:20]):
            gaps.append({"model_id": model_id, "scene_type": scene_type, "data_points": 0})

    if gaps:
        names = ", ".join(g["model_id"] for g in gaps)
        summary = f"{names} 在 {scene_type} 场景历史数据不足（< 3 数据点）"
    else:
        summary = f"所有候选模型在 {scene_type} 场景均有充足历史数据"

    return {"gaps": gaps, "summary": summary}


def read_model_profile(model_id: str) -> dict:
    """读取模型的 profile.md + tag_tutorial.md"""
    base = KB_ROOT / "models" / model_id
    result = {"model_id": model_id, "profile": "", "tag_tutorial": ""}
    if not base.exists():
        return result
    for key, fname in [("profile", "profile.md"), ("tag_tutorial", "tag_tutorial.md")]:
        fpath = base / fname
        if fpath.exists():
            result[key] = fpath.read_text(encoding="utf-8")
    return result


def list_experiences(scene_type: str | None = None, limit: int = 5) -> list[str]:
    """列出经验文件路径，可按 scene_type 过滤，返回最近 N 个（按文件名倒序）"""
    _ensure_kb()
    exp_dir = KB_ROOT / "experiences"
    if not exp_dir.exists():
        return []
    if scene_type:
        target = exp_dir / scene_type
        if not target.exists():
            return []
        files = sorted(target.glob("*.md"), reverse=True)
    else:
        files = sorted(exp_dir.rglob("*.md"), reverse=True)
    paths = [str(f.relative_to(KB_ROOT)) for f in files[:limit]]
    return paths


def write_experience_file(task_id: str, scene_type: str, content: str) -> str:
    """写入经验文件（追加模式），返回写入的相对路径。"""
    with _kb_write_lock:
        _ensure_kb()
        dir_path = KB_ROOT / "experiences" / scene_type
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"{date.today().isoformat()}_{task_id[:8]}.md"
        fpath = dir_path / filename
        if fpath.exists():
            with fpath.open("a", encoding="utf-8") as f:
                f.write(f"\n\n---\n\n{content}")
        else:
            fpath.write_text(content, encoding="utf-8")
        return str(fpath.relative_to(KB_ROOT))


def update_coverage_matrix(scene_type: str, model_id: str, avg_rank: float, n_rounds: int):
    """更新覆盖矩阵 markdown 文件中的数据。"""
    with _kb_write_lock:
        _ensure_kb()
        coverage_dir = KB_ROOT / "coverage"
        coverage_dir.mkdir(parents=True, exist_ok=True)
        matrix_path = coverage_dir / "model_scene_matrix.md"

        if not matrix_path.exists():
            header = f"# 覆盖度矩阵\n\n更新时间：{date.today().isoformat()}\n\n"
            header += f"| model | {scene_type} |\n|-------|-------|\n"
            header += f"| {model_id} | {n_rounds} 任务, avg_rank {avg_rank:.1f} |\n"
            matrix_path.write_text(header, encoding="utf-8")
            return

        content = matrix_path.read_text(encoding="utf-8")
        content = re.sub(r"更新时间：\d{4}-\d{2}-\d{2}", f"更新时间：{date.today().isoformat()}", content)

        pattern = rf"(\|\s*{re.escape(model_id)}\s*\|)"
        if re.search(pattern, content):
            scene_cell = f"{n_rounds} 任务, avg_rank {avg_rank:.1f}"
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if re.match(rf"\|\s*{re.escape(model_id)}\s*\|", line):
                    if scene_type in line:
                        lines[i] = re.sub(
                            rf"(\|\s*){re.escape(scene_type)}[^|]*",
                            f"\\1{scene_cell}",
                            line
                        )
                    break
            content = "\n".join(lines)
        else:
            insert = f"| {model_id} | {n_rounds} 任务, avg_rank {avg_rank:.1f} |\n"
            content = content.rstrip("\n") + "\n" + insert

        matrix_path.write_text(content, encoding="utf-8")


def list_factor_index() -> dict:
    """返回所有 factor 的轻量目录，不含全文。
    解析每个 factor 文件的 frontmatter 和 ## 规律 段。

    返回结构：
      {
        "factors": [
          {"factor_key", "factor_type", "confidence", "rule", "file",
           "factor_subject", "factor_dimension", "factor_scene"}   # 新格式文件才有后三个
        ],
        "known_dimensions": ["跨轮稳定性", "相似度", ...]   # 已有 dimension 去重列表
      }
    """
    _ensure_kb()
    factor_files = glob_kb("**/*.md", "factors")
    index = []
    for fp in factor_files:
        content = read_kb_file(fp)
        if not content:
            continue
        meta = {
            "factor_key": "", "factor_type": "", "confidence": "",
            "rule": "", "file": fp,
            "factor_subject": "", "factor_dimension": "", "factor_scene": "",
        }
        lines = content.split("\n")
        in_front = False
        after_rule = False
        for line in lines:
            stripped = line.strip()
            if stripped == "---":
                in_front = not in_front
                continue
            if in_front:
                if stripped.startswith("factor_key:"):
                    meta["factor_key"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("factor_type:"):
                    meta["factor_type"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("confidence:"):
                    meta["confidence"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("factor_subject:"):
                    meta["factor_subject"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("factor_dimension:"):
                    meta["factor_dimension"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("factor_scene:"):
                    meta["factor_scene"] = stripped.split(":", 1)[1].strip()
            if stripped == "## 规律":
                after_rule = True
                continue
            if after_rule and stripped:
                meta["rule"] = stripped
                break

        # 旧格式文件没有 factor_dimension，从 factor_key 启发式解析最后一段作为 dimension
        if not meta["factor_dimension"] and meta["factor_key"]:
            parts = meta["factor_key"].replace("_", "-").split("-")
            if len(parts) >= 2:
                meta["factor_dimension"] = parts[-1]

        index.append(meta)

    # 收集所有已知 dimension，去重排序
    known_dimensions = sorted({
        e["factor_dimension"] for e in index if e["factor_dimension"]
    })

    return {"factors": index, "known_dimensions": known_dimensions}


def write_strategy_file(task_id: str, scene_type: str, portfolio: list) -> str:
    """将任务最终策略档案持久化到 kb/strategies/{scene_type}/{date}_{task_id[:8]}.json。"""
    _ensure_kb()
    dir_path = KB_ROOT / "strategies" / scene_type
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = f"{date.today().isoformat()}_{task_id[:8]}.json"
    fpath = dir_path / filename
    fpath.write_text(json.dumps({
        "task_id": task_id,
        "date": date.today().isoformat(),
        "scene_type": scene_type,
        "strategies": portfolio,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(fpath.relative_to(KB_ROOT))


def append_model_profile(model_id: str, profile_finding: str, tag_finding: str = "") -> dict:
    """以追加模式将实验新发现写入 kb/models/{model_id}/profile.md（和 tag_tutorial.md）。"""
    with _kb_write_lock:
        _ensure_kb()
        base = KB_ROOT / "models" / model_id
        base.mkdir(parents=True, exist_ok=True)

        separator = f"\n\n---\n### 实验发现 {date.today().isoformat()}\n"
        result = {}

        profile_path = base / "profile.md"
        if profile_path.exists():
            with profile_path.open("a", encoding="utf-8") as f:
                f.write(separator + profile_finding)
        else:
            profile_path.write_text(f"# {model_id} 模型档案\n\n{profile_finding}", encoding="utf-8")
        result["profile"] = str(profile_path.relative_to(KB_ROOT))

        if tag_finding:
            tag_path = base / "tag_tutorial.md"
            if tag_path.exists():
                with tag_path.open("a", encoding="utf-8") as f:
                    f.write(separator + tag_finding)
            else:
                tag_path.write_text(f"# {model_id} Tag 使用指南\n\n{tag_finding}", encoding="utf-8")
            result["tag_tutorial"] = str(tag_path.relative_to(KB_ROOT))

        return result
