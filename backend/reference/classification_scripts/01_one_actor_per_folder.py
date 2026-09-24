"""
参考脚本 01：flat 结构
输入：每个顶层目录是一个角色目录，音频文件直接在其中（无 audio/ 子目录）
输出：output_path/{角色名}/audio/*.wav

适用场景：H41 型——original_paths 里每个元素就是角色目录。
"""
import os
import shutil
from pathlib import Path

AUDIO_EXTS = {'.wav', '.mp3', '.flac'}

# output_path 和 original_paths 由执行器注入
for role_path_str in original_paths:
    role_path = Path(role_path_str)
    if not role_path.is_dir():
        continue
    role_name = role_path.name
    dst_audio = Path(output_path) / role_name / 'audio'
    os.makedirs(str(dst_audio), exist_ok=True)
    for audio_file in sorted(role_path.iterdir()):
        if audio_file.is_file() and audio_file.suffix.lower() in AUDIO_EXTS:
            shutil.move(str(audio_file), str(dst_audio / audio_file.name))
