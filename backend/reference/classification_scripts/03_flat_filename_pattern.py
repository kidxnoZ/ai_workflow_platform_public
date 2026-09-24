"""
参考脚本 03：根目录平铺，角色名编码在文件名中
输入：所有音频文件直接在源目录根目录，文件名格式 {前缀}_{角色名}_{序号}.wav
输出：output_path/{角色名}/audio/*.wav

适用场景：G126 型——original_paths[0] 是包含所有音频的根目录，
角色名从文件名中提取，提取规则需根据实际数据集调整。
"""
import os
import shutil
from pathlib import Path

AUDIO_EXTS = {'.wav', '.mp3', '.flac'}

# ── 角色名提取规则（根据数据集命名规律调整此函数）────────────────────────────
def extract_role_name(stem: str) -> str:
    """
    示例规则：文件名 DX_RoleName_001 → 取第 2 段（下划线分割）。
    若无法解析，归入 _special/unclassified/。
    """
    parts = stem.split('_')
    if len(parts) >= 3:
        return parts[1]
    return None  # 无法识别 → unclassified


# output_path 和 original_paths 由执行器注入
for src_path_str in original_paths:
    src_path = Path(src_path_str)
    if not src_path.is_dir():
        continue
    for audio_file in sorted(src_path.iterdir()):
        if not audio_file.is_file() or audio_file.suffix.lower() not in AUDIO_EXTS:
            continue
        role_name = extract_role_name(audio_file.stem)
        if role_name:
            dst_audio = Path(output_path) / role_name / 'audio'
        else:
            dst_audio = Path(output_path) / '_special' / 'unclassified'
        os.makedirs(str(dst_audio), exist_ok=True)
        shutil.move(str(audio_file), str(dst_audio / audio_file.name))
