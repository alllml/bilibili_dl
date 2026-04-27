"""
B 站视频解析插件

【设计模式】具体策略类 ——
继承 BaseExtractor，实现 B 站平台的视频解析逻辑。
阶段三：真实请求 B 站 API 获取视频元数据和 Dash 流 URL。
"""
import os
import re
import subprocess
import tempfile

import requests

from core.base_extractor import BaseExtractor, VideoInfo
from core.downloader import download_file


# B 站 API 地址
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"

# 清晰度 ID → 名称映射
_QUALITY_MAP = {
    127: "8K", 126: "杜比视界", 125: "HDR",
    120: "4K", 116: "1080P60", 112: "1080P+",
    80: "1080P", 64: "720P", 32: "480P", 16: "360P",
}

# 通用请求头
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}


class BilibiliExtractor(BaseExtractor):
    """
    B 站视频解析器。
    支持 bilibili.com 主站链接和 b23.tv 短链（短链由工厂层还原）。
    """

    SUPPORTED_DOMAINS = ["bilibili.com", "b23.tv", "m.bilibili.com"]
    PLATFORM_ID = "bilibili"
    PLATFORM_NAME = "B站"
    REQUIRES_COOKIE = True

    def _extract_bvid(self, url: str) -> str | None:
        """从 URL 中用正则提取 BV 号（BV + 10 位字母数字）"""
        match = re.search(r"(BV[a-zA-Z0-9]{10})", url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _extract_avid(self, url: str) -> str | None:
        """从 URL 中提取 AV 号"""
        match = re.search(r"av(\d+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _build_headers(self, cookie: str = "") -> dict:
        """构造请求头，可选附加 Cookie"""
        headers = dict(_DEFAULT_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def extract_info(self, url: str, cookie: str = "") -> VideoInfo:
        """
        真实解析 B 站视频信息：
        1. 提取 BV 号或 AV 号
        2. 请求 view API 获取标题、封面、cid、UP主、时长等
        3. 请求 playurl API 获取 Dash 流列表
        """
        # 尝试提取 BV 号或 AV 号
        bvid = self._extract_bvid(url)
        avid = self._extract_avid(url)

        if not bvid and not avid:
            raise ValueError(f"无法从 URL 中提取 BV 号或 AV 号: {url}")

        headers = self._build_headers(cookie)
        print(f"正在使用 BilibiliExtractor 处理链接...")

        # 构造 API 参数
        params = {}
        if bvid:
            params["bvid"] = bvid
            print(f"  BV号: {bvid}")
        else:
            params["aid"] = avid
            print(f"  AV号: av{avid}")

        # ---- 第一步：获取视频基本信息 ----
        resp = requests.get(_VIEW_API, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        view_data = resp.json()

        if view_data.get("code") != 0:
            raise RuntimeError(f"B站 API 返回错误: {view_data.get('message', '未知错误')}")

        data = view_data["data"]
        title = data["title"]
        cover_url = data["pic"]
        # 取第一个分P的 cid（多P视频暂只处理第一P）
        cid = data["pages"][0]["cid"]
        # 提取视频元数据
        duration = data.get("duration", 0)
        owner = data.get("owner", {}).get("name", "")
        stat = data.get("stat", {})
        view_count = stat.get("view", 0)
        danmaku_count = stat.get("danmaku", 0)
        # 确保有 bvid 用于后续 playurl 请求
        if not bvid:
            bvid = data.get("bvid", "")

        print(f"  标题: {title}")
        print(f"  UP主: {owner}")
        print(f"  时长: {duration}秒")
        print(f"  封面: {cover_url}")
        print(f"  CID: {cid}")

        # ---- 第二步：获取 Dash 流地址 ----
        # fnval=16 请求 Dash 格式
        playurl_params = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,
            "fourk": 1,
        }
        resp = requests.get(_PLAYURL_API, params=playurl_params, headers=headers, timeout=10)
        resp.raise_for_status()
        playurl_data = resp.json()

        if playurl_data.get("code") != 0:
            raise RuntimeError(f"B站播放地址 API 错误: {playurl_data.get('message', '未知错误')}")

        dash = playurl_data["data"].get("dash")
        if not dash:
            raise RuntimeError("未获取到 Dash 流数据，该视频可能不支持 Dash 格式")

        # 解析视频流列表
        video_streams = []
        print("\n  可用视频流:")
        for v in dash.get("video", []):
            quality_name = _QUALITY_MAP.get(v["id"], f"未知({v['id']})")
            stream_info = {
                "quality_id": v["id"],
                "quality_name": quality_name,
                "url": v["baseUrl"],
                "codecs": v.get("codecs", ""),
                "bandwidth": v.get("bandwidth", 0),
                "type": "video",
            }
            video_streams.append(stream_info)
            print(f"    {quality_name} | {v.get('codecs', '')} | {v.get('bandwidth', 0)//1000}kbps")

        # 解析音频流列表
        audio_streams = []
        print("\n  可用音频流:")
        for a in dash.get("audio", []):
            stream_info = {
                "quality_id": a["id"],
                "url": a["baseUrl"],
                "codecs": a.get("codecs", ""),
                "bandwidth": a.get("bandwidth", 0),
                "type": "audio",
            }
            audio_streams.append(stream_info)
            print(f"    音频 {a['id']} | {a.get('codecs', '')} | {a.get('bandwidth', 0)//1000}kbps")

        # 选取最高画质视频流和最高音质音频流的 URL
        best_video_url = video_streams[0]["url"] if video_streams else ""
        best_audio_url = audio_streams[0]["url"] if audio_streams else ""

        # Cookie 提示
        if not cookie:
            print("\n  [提示] 未配置 Cookie，当前仅能获取低清晰度视频流")

        return VideoInfo(
            title=title,
            cover_url=cover_url,
            video_url=best_video_url,
            audio_url=best_audio_url,
            streams=video_streams + audio_streams,
            headers=dict(_DEFAULT_HEADERS),
            platform="bilibili",
            duration=duration,
            owner=owner,
            view_count=view_count,
            danmaku_count=danmaku_count,
        )

    def download(self, video_info: VideoInfo, save_path: str, capabilities: dict,
                 progress_callback=None) -> None:
        """
        B 站视频下载编排：
        - FFmpeg 可用：分别下载视频流 + 音频流，合并为 MP4
        - FFmpeg 不可用：仅下载视频流（画质较低，无独立音轨）
        进度分配：视频 0-80%，音频 80-95%，合并 95-100%
        """
        headers = video_info.get("headers", dict(_DEFAULT_HEADERS))
        video_url = video_info.get("video_url", "")
        audio_url = video_info.get("audio_url", "")
        ffmpeg_available = capabilities.get("ffmpeg_available", False)
        ffmpeg_path = capabilities.get("ffmpeg_path", "ffmpeg")

        if not video_url:
            raise RuntimeError("未获取到视频流地址，无法下载")

        if ffmpeg_available and audio_url:
            # ---- Dash 模式：视频 + 音频分别下载后合并 ----
            tmp_dir = tempfile.mkdtemp(prefix="bilibili_dl_")
            tmp_video = os.path.join(tmp_dir, "video.m4s")
            tmp_audio = os.path.join(tmp_dir, "audio.m4s")

            try:
                # 下载视频流（进度 0-80%）
                def video_progress(downloaded, total):
                    if progress_callback:
                        progress_callback(int(downloaded * 0.8), int(total * 0.8) if total else 0)

                print("  正在下载视频流...")
                download_file(video_url, headers, tmp_video, progress_callback=video_progress)

                # 下载音频流（进度 80-95%）
                def audio_progress(downloaded, total):
                    if progress_callback and total:
                        ratio = downloaded / total
                        progress_callback(int((0.8 + ratio * 0.15) * total), total)

                print("  正在下载音频流...")
                download_file(audio_url, headers, tmp_audio, progress_callback=audio_progress)

                # FFmpeg 合并（进度 95-100%）
                print("  正在合并音视频...")
                cmd = [
                    ffmpeg_path,
                    "-i", tmp_video,
                    "-i", tmp_audio,
                    "-c", "copy",
                    save_path,
                    "-y",
                ]
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                result = subprocess.run(cmd, capture_output=True, timeout=300,
                                        creationflags=creationflags, encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    stderr = result.stderr or ""
                    raise RuntimeError(f"FFmpeg 合并失败: {stderr[:500]}")

                if progress_callback:
                    progress_callback(100, 100)
                print(f"  下载完成: {save_path}")

            finally:
                # 清理临时文件
                for f in (tmp_video, tmp_audio):
                    if os.path.exists(f):
                        os.remove(f)
                if os.path.exists(tmp_dir):
                    os.rmdir(tmp_dir)
        else:
            # ---- 降级模式：仅下载视频流 ----
            print("  FFmpeg 不可用，仅下载视频流（无独立音轨）...")
            download_file(video_url, headers, save_path, progress_callback=progress_callback)
            print(f"  下载完成: {save_path}")
