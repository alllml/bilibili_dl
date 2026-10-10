"""
媒体处理工具（基于 FFmpeg）

抽出来给各插件共用：
- 从任意媒体文件中抽取音频并转成 MP3（用于「仅下载音频」）；
- 把 B站弹幕 XML 转成可读的 ASS 略过（直接保存原始 XML 即可）。
"""
import os
import subprocess


def _creation_flags() -> int:
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def ffmpeg_extract_audio(ffmpeg_path: str, src_path: str, out_path: str,
                         timeout: int = 900) -> None:
    """
    用 FFmpeg 从 src_path 中抽取音频并编码为 MP3 保存到 out_path。
    失败时抛 RuntimeError（附带 ffmpeg 的错误输出，便于排查）。
    """
    if not ffmpeg_path:
        raise RuntimeError("「仅下载音频」需要 FFmpeg，请先在 设置 → 指定 FFmpeg 路径 中配置")

    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        out_path,
    ]
    result = subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        creationflags=_creation_flags(), encoding="utf-8", errors="replace")

    if result.returncode != 0 or not os.path.exists(out_path):
        stderr = (result.stderr or "")[:500]
        raise RuntimeError(f"FFmpeg 提取音频失败: {stderr or '未知错误'}")


def mp3_path_for(video_path: str) -> str:
    """由视频文件路径推导出同名的 mp3 路径"""
    base, _ = os.path.splitext(video_path)
    return base + ".mp3"
