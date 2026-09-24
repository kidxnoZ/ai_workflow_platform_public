"""backend/tools/classification.py

Directory scanning, reference script loading, classification script execution,
role list and dir-tree generation, MERGE_TASKS application.

Depends on sandbox.py for actual script execution.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from backend.tools.sandbox import SandboxConfig, ExecutionResult, run_agent_code
from backend.config import STORAGE_DIR


REFERENCE_SCRIPTS_DIR = Path(__file__).parent.parent / "reference" / "classification_scripts"
EXPERIENCE_FILE = STORAGE_DIR / "reference" / "classification_experience.md"

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac"}


# ── Safety helpers ────────────────────────────────────────────────────────────

def _assert_in_bounds(path: str, root: str) -> None:
    """Raise ValueError if path resolves outside root (symlink-safe)."""
    if not Path(path).resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"Path out of bounds: {path}")


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class DirectoryStructure:
    output_path: str
    structure_type: str          # "cv_hierarchy" | "flat"
    sample_filenames: dict[str, list[str]]   # dir → [filenames]
    original_paths: list[str]    # all top-level paths before classification
    total_audio_files: int


def count_audio_files(path: str) -> int:
    """Recursively count .wav, .mp3, .flac files under *path*.
    Returns 0 if the path does not exist.
    """
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return 1 if p.suffix.lower() in AUDIO_EXTENSIONS else 0
    total = 0
    for entry in p.iterdir():
        if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
            total += 1
        elif entry.is_dir():
            total += count_audio_files(str(entry))
    return total


def detect_structure_type(output_path: str) -> str:
    """Detect the dataset layout.

    Returns ``"cv_hierarchy"`` if top-level entries are directories and at least
    one of them contains subdirectories (i.e. no audio files directly in root).
    Returns ``"flat"`` when audio files exist directly in the root directory.
    """
    p = Path(output_path)
    if not p.is_dir():
        return "flat"

    # Check for audio files directly in root → flat
    for entry in p.iterdir():
        if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
            return "flat"

    # Check for cv_hierarchy: top-level dirs exist and at least one has a subdir
    has_top_level_dir = False
    has_subdir = False
    for entry in p.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            has_top_level_dir = True
            for child in entry.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    has_subdir = True
                    break
            if has_subdir:
                break

    if has_top_level_dir and has_subdir:
        return "cv_hierarchy"

    return "flat"


def scan_directory(output_path: str) -> DirectoryStructure:
    """Walk up to 3 levels deep, collect up to 5 sample filenames per
    directory, detect structure type, and count total audio files.
    """
    p = Path(output_path)

    # Record original top-level paths before any modification
    original_paths: list[str] = []
    if p.is_dir():
        for entry in sorted(p.iterdir()):
            original_paths.append(str(entry))

    # Walk up to 3 levels, collect sample filenames
    sample_filenames: dict[str, list[str]] = {}

    def _walk(dir_path: Path, depth: int) -> None:
        if depth > 3:
            return
        if not dir_path.is_dir():
            return
        key = str(dir_path)
        samples: list[str] = []
        for entry in sorted(dir_path.iterdir()):
            if entry.name.startswith("."):
                continue
            if len(samples) >= 5:
                break
            samples.append(entry.name)
        if samples:
            sample_filenames[key] = samples
        else:
            sample_filenames[key] = []

        # Recurse into subdirectories (skip hidden)
        for entry in sorted(dir_path.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                _walk(entry, depth + 1)

    if p.is_dir():
        _walk(p, 1)

    structure_type = detect_structure_type(output_path)
    total_audio_files = count_audio_files(output_path)

    return DirectoryStructure(
        output_path=output_path,
        structure_type=structure_type,
        sample_filenames=sample_filenames,
        original_paths=original_paths,
        total_audio_files=total_audio_files,
    )


def load_reference_scripts() -> str:
    """Read all .py files from ``REFERENCE_SCRIPTS_DIR`` and concatenate them
    with ``# --- {filename} ---`` headers.  Returns an empty string when the
    directory does not exist or contains no .py files.
    """
    ref_dir = REFERENCE_SCRIPTS_DIR
    if not ref_dir.is_dir():
        return ""

    parts: list[str] = []
    for py_file in sorted(ref_dir.glob("*.py")):
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        parts.append(f"# --- {py_file.name} ---\n{content}")

    return "\n".join(parts)


def load_classification_experience() -> str:
    """Read ``EXPERIENCE_FILE``.  Returns ``""`` if the file is missing."""
    ef = EXPERIENCE_FILE
    if not ef.is_file():
        return ""
    try:
        return ef.read_text(encoding="utf-8")
    except Exception:
        return ""


def append_experience_entry(entry: str) -> None:
    """Append *entry* to ``EXPERIENCE_FILE``.

    Creates the file (and parent directories) if it does not exist.  A newline
    is inserted before *entry* when the file already has content.
    """
    ef = EXPERIENCE_FILE
    ef.parent.mkdir(parents=True, exist_ok=True)

    if ef.is_file():
        existing = ef.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            ef.write_text(existing + "\n" + entry + "\n", encoding="utf-8")
        elif existing:
            ef.write_text(existing + entry + "\n", encoding="utf-8")
        else:
            ef.write_text(entry + "\n", encoding="utf-8")
    else:
        ef.write_text(entry + "\n", encoding="utf-8")


def execute_classification_script(
    script: str,
    config: SandboxConfig,
    dry_run: bool = True,
) -> ExecutionResult:
    """Run *script* through the sandbox.  Delegates to ``run_agent_code``
    from ``sandbox.py``, which always performs a dry-run first internally.
    """
    return run_agent_code(script, config)


def generate_role_list(output_path: str) -> str:
    """Build a tab-separated role-to-file-count list sorted descending by count.

    Scans subdirectories of *output_path* that contain an ``audio/`` subdir,
    writes the result to ``{output_path}/_role_list.txt``, and returns the
    file content as a string.
    """
    p = Path(output_path)
    if not p.is_dir():
        content = ""
        role_file = p / "_role_list.txt"
        role_file.parent.mkdir(parents=True, exist_ok=True)
        role_file.write_text(content, encoding="utf-8")
        return content

    roles: list[tuple[str, int]] = []
    for entry in sorted(p.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        audio_dir = entry / "audio"
        if audio_dir.is_dir():
            count = count_audio_files(str(audio_dir))
            roles.append((entry.name, count))

    # Sort by count descending, then by name ascending for stability
    roles.sort(key=lambda x: (-x[1], x[0]))

    lines = [f"{role}\t{count}" for role, count in roles]
    content = "\n".join(lines)
    if content:
        content += "\n"

    role_file = p / "_role_list.txt"
    role_file.write_text(content, encoding="utf-8")

    return content


def generate_dir_tree(output_path: str) -> dict:
    """Build a JSON-serialisable nested directory tree.

    Each node: ``{"name", "type", "file_count", "children"}``.
    Walks up to 3 levels deep.  Skips hidden entries.
    """
    p = Path(output_path)

    def _build(node_path: Path, depth: int) -> dict:
        node: dict = {
            "name": node_path.name or str(node_path),
            "type": "dir" if node_path.is_dir() else "file",
            "file_count": None,
            "children": [],
        }

        if not node_path.is_dir():
            return node

        # file_count for speaker dirs = audio files in their audio/ subdir
        audio_dir = node_path / "audio"
        if audio_dir.is_dir():
            node["file_count"] = count_audio_files(str(audio_dir))
        else:
            # Count audio files directly in this directory
            direct_count = 0
            for entry in node_path.iterdir():
                if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
                    direct_count += 1
            if direct_count > 0:
                node["file_count"] = direct_count

        if depth >= 3:
            return node

        children: list[dict] = []
        for entry in sorted(node_path.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
            if entry.name.startswith("."):
                continue
            children.append(_build(entry, depth + 1))

        node["children"] = children
        return node

    if p.is_dir():
        return _build(p, 0)
    return _build(p, 0)


def apply_merge_tasks(
    merge_tasks: list[dict],
    output_path: str,
    config: SandboxConfig,
) -> None:
    """Execute explicit speaker-merges by moving audio files.

    Each task: ``{"src_speaker": "苏星文战斗", "dst_speaker": "苏星文"}``.

    1. Validate both paths are within *output_path*.
    2. Move every audio file from ``src_speaker/audio/`` into
       ``dst_speaker/audio/``.
    3. On filename collision append ``_merged_{n}`` before the extension.
    4. Remove the now-empty src speaker directory.
    """
    import shutil

    root = str(Path(output_path).resolve())

    for task in merge_tasks:
        src_speaker = task["src_speaker"]
        dst_speaker = task["dst_speaker"]

        src_dir = Path(output_path) / src_speaker
        dst_dir = Path(output_path) / dst_speaker
        src_audio = src_dir / "audio"
        dst_audio = dst_dir / "audio"

        # Bounds checks
        _assert_in_bounds(str(src_audio), root)
        _assert_in_bounds(str(dst_audio), root)

        # Ensure destination audio dir exists
        dst_audio.mkdir(parents=True, exist_ok=True)
        _assert_in_bounds(str(dst_audio), root)  # re-check after creation

        # List source audio files
        if not src_audio.is_dir():
            continue

        for item in sorted(src_audio.iterdir()):
            if not item.is_file():
                continue
            if item.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            src_file = item
            dst_file = dst_audio / item.name

            # Handle filename collision
            if dst_file.exists():
                stem = item.stem
                suffix = item.suffix
                n = 1
                while True:
                    new_name = f"{stem}_merged_{n}{suffix}"
                    candidate = dst_audio / new_name
                    if not candidate.exists():
                        dst_file = candidate
                        break
                    n += 1

            _assert_in_bounds(str(src_file), root)
            _assert_in_bounds(str(dst_file), root)
            shutil.move(str(src_file), str(dst_file))

        # Remove src speaker dir only if it is empty after the move
        _try_remove_empty_dir(src_dir)


def _try_remove_empty_dir(dir_path: Path) -> None:
    """Remove *dir_path* if it is empty after cleaning up any empty
    child directories recursively (bottom-up).
    """
    if not dir_path.is_dir():
        return

    # Recurse into children first (bottom-up removal)
    try:
        for child in dir_path.iterdir():
            if child.is_dir():
                _try_remove_empty_dir(child)
    except OSError:
        return

    # Now check if dir_path itself is empty
    try:
        remaining = list(dir_path.iterdir())
    except OSError:
        return

    if remaining:
        return  # still has content

    try:
        dir_path.rmdir()
    except OSError:
        return
