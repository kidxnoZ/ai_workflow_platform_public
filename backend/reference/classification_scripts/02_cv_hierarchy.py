"""
参考脚本 02：cv_hierarchy 结构
输入：一级 CV 目录 → 二级角色目录 → 音频文件（无 audio/ 子目录）
输出：output_path/{角色名}/audio/*.wav  （CV 层被展平，角色名直接在 output_path 下）

适用场景：H42 型——original_paths 里每个元素是一个 CV 目录，
CV 目录下的子目录是角色目录，角色目录下直接是音频文件。

关键：不能只处理 original_paths[0]，必须遍历所有 CV 目录。
"""
import os
import shutil
from pathlib import Path

AUDIO_EXTS = {'.wav', '.mp3', '.flac'}

# output_path 和 original_paths 由执行器注入
for cv_path_str in original_paths:
    cv_path = Path(cv_path_str)
    if not cv_path.is_dir():
        continue
    for role_dir in sorted(cv_path.iterdir()):
        if not role_dir.is_dir():
            continue
        role_name = role_dir.name
        dst_audio = Path(output_path) / role_name / 'audio'
        os.makedirs(str(dst_audio), exist_ok=True)
        for audio_file in sorted(role_dir.iterdir()):
            if audio_file.is_file() and audio_file.suffix.lower() in AUDIO_EXTS:
                shutil.move(str(audio_file), str(dst_audio / audio_file.name))
        # 移完后删除已空的原角色目录
        try:
            role_dir.rmdir()
        except OSError:
            pass
    # 移完后删除已空的 CV 目录
    try:
        cv_path.rmdir()
    except OSError:
        pass
