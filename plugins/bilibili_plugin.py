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
import time
from urllib.parse import urlparse, parse_qs

import requests

from core.base_extractor import BaseExtractor, VideoInfo
from core.downloader import download_file, DownloadCancelled, probe_fastest_url
from core.media import ffmpeg_extract_audio


def _http_get(url, params=None, headers=None, timeout=15, attempts=3):
    """
    带重试的 GET（用于解析阶段的 API 请求）。

    走 VPN / 代理时，到 api.bilibili.com 的连接经常被中途重置
    （SSLEOFError / ConnectionError / 超时），一次失败就放弃会导致"解析失败"。
    这里对**网络类错误**自动重试；HTTP 4xx/5xx（如风控 412）不重试、直接抛。
    """
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if i < attempts:
                time.sleep(0.8 * i)
                print(f"  请求失败（{type(e).__name__}），第 {i + 1} 次重试...")
    raise last_exc


# B 站 API 地址
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
_PLAYER_V2_API = "https://api.bilibili.com/x/player/v2"
_DANMAKU_API = "https://comment.bilibili.com/{cid}.xml"


def _collect_cdn_urls(stream: dict) -> list:
    """
    收集一个 Dash 流的所有 CDN 地址（baseUrl 优先，其后是 backupUrl）。
    B站会返回多个不同 CDN 的地址，某个 CDN 慢/被掐时可自动切换。
    """
    urls = []
    for key in ("baseUrl", "base_url"):
        value = stream.get(key)
        if value and value not in urls:
            urls.append(value)
    for key in ("backupUrl", "backup_url"):
        for value in stream.get(key) or []:
            if value and value not in urls:
                urls.append(value)
    return urls


def _srt_time(seconds: float) -> str:
    """秒 → SRT 时间戳（00:00:00,000）"""
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    s = total_ms // 1000
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _subtitle_to_srt(body: list) -> str:
    """把 B站字幕 JSON（body 数组）转换为 SRT 文本"""
    lines = []
    index = 1
    for item in body or []:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        start = item.get("from", 0) or 0
        end = item.get("to", 0) or 0
        lines.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{content}\n")
        index += 1
    return "\n".join(lines)

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

    SUPPORTED_DOMAINS = ["bilibili.com", "b23.tv", "bili2233.cn"]
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
        resp = _http_get(_VIEW_API, params=params, headers=headers)
        view_data = resp.json()

        if view_data.get("code") != 0:
            raise RuntimeError(f"B站 API 返回错误: {view_data.get('message', '未知错误')}")

        data = view_data["data"]
        title = data["title"]
        cover_url = data["pic"]

        # ---- 分P处理：读取 URL 中的 ?p=N，选中对应分P ----
        page_index = 1
        try:
            query = parse_qs(urlparse(url).query)
            if "p" in query:
                page_index = max(1, int(query["p"][0]))
        except (ValueError, TypeError):
            page_index = 1

        pages_raw = data.get("pages") or []
        if not pages_raw:
            raise RuntimeError("B站返回数据中没有分P信息")
        page_index = min(page_index, len(pages_raw))
        current_page = pages_raw[page_index - 1]
        cid = current_page["cid"]

        # 供 GUI 展示的分P列表
        page_list = [
            {
                "index": pg.get("page", i + 1),
                "title": pg.get("part", f"P{i + 1}"),
                "cid": pg.get("cid"),
                "duration": pg.get("duration", 0),
            }
            for i, pg in enumerate(pages_raw)
        ]

        # 多P视频在标题后追加 P 号，避免不同分P的文件名互相覆盖
        if len(pages_raw) > 1:
            title = f"{title} - P{page_index}"

        # 提取视频元数据
        duration = current_page.get("duration", data.get("duration", 0))
        owner = data.get("owner", {}).get("name", "")
        stat = data.get("stat", {})
        view_count = stat.get("view", 0)
        danmaku_count = stat.get("danmaku", 0)
        # 确保有 bvid 用于后续 playurl 请求
        if not bvid:
            bvid = data.get("bvid", "")

        print(f"  分P: 第 {page_index}/{len(pages_raw)} P")

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
        resp = _http_get(_PLAYURL_API, params=playurl_params, headers=headers)
        playurl_data = resp.json()

        if playurl_data.get("code") != 0:
            raise RuntimeError(f"B站播放地址 API 错误: {playurl_data.get('message', '未知错误')}")

        dash = playurl_data["data"].get("dash")
        if not dash:
            raise RuntimeError("未获取到 Dash 流数据，该视频可能不支持 Dash 格式")

        # 解析视频流列表
        # 【重要】B站每个流都有 baseUrl + backupUrl（不同 CDN：阿里云/腾讯云/华为/Akamai…）。
        # baseUrl 可能命中又慢又容易被掐连接的 CDN（实测 os=akam 会反复 SSLEOFError），
        # 所以这里把所有 CDN 地址都收集起来，失败时自动切换。
        video_streams = []
        print("\n  可用视频流:")
        for v in dash.get("video", []):
            quality_name = _QUALITY_MAP.get(v["id"], f"未知({v['id']})")
            urls = _collect_cdn_urls(v)
            stream_info = {
                "quality_id": v["id"],
                "quality_name": quality_name,
                "url": urls[0] if urls else "",
                "urls": urls,
                "codecs": v.get("codecs", ""),
                "bandwidth": v.get("bandwidth", 0),
                "type": "video",
            }
            video_streams.append(stream_info)
            print(f"    {quality_name} | {v.get('codecs', '')} | "
                  f"{v.get('bandwidth', 0)//1000}kbps | {len(urls)} 个 CDN 候选")

        # 解析音频流列表
        audio_streams = []
        print("\n  可用音频流:")
        for a in dash.get("audio", []):
            urls = _collect_cdn_urls(a)
            stream_info = {
                "quality_id": a["id"],
                "url": urls[0] if urls else "",
                "urls": urls,
                "codecs": a.get("codecs", ""),
                "bandwidth": a.get("bandwidth", 0),
                "type": "audio",
            }
            audio_streams.append(stream_info)
            print(f"    音频 {a['id']} | {a.get('codecs', '')} | "
                  f"{a.get('bandwidth', 0)//1000}kbps | {len(urls)} 个 CDN 候选")

        # 选取最高画质视频流和最高音质音频流的 URL（含全部 CDN 候选）
        best_video_url = video_streams[0]["url"] if video_streams else ""
        best_audio_url = audio_streams[0]["url"] if audio_streams else ""
        video_urls = video_streams[0]["urls"] if video_streams else []
        audio_urls = audio_streams[0]["urls"] if audio_streams else []

        # Cookie 提示
        if not cookie:
            print("\n  [提示] 未配置 Cookie，当前仅能获取低清晰度视频流")

        return VideoInfo(
            title=title,
            cover_url=cover_url,
            video_url=best_video_url,
            audio_url=best_audio_url,
            video_urls=video_urls,
            audio_urls=audio_urls,
            streams=video_streams + audio_streams,
            headers=dict(_DEFAULT_HEADERS),
            platform="bilibili",
            duration=duration,
            owner=owner,
            view_count=view_count,
            danmaku_count=danmaku_count,
            pages=page_list,
            bvid=bvid,
            cid=cid,
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
        cancel_event = capabilities.get("cancel_event")
        connections = capabilities.get("connections", 1) or 1
        # 「自动」模式（默认）：并发由下载器按实时吞吐自适应；否则固定为设定值
        adaptive = bool(capabilities.get("adaptive_connections", True))

        # 每个流的全部 CDN 候选（baseUrl + backupUrl）；某个 CDN 被掐连接时自动换下一个
        video_candidates = video_info.get("video_urls") or ([video_url] if video_url else [])
        audio_candidates = video_info.get("audio_urls") or ([audio_url] if audio_url else [])

        if not video_url:
            raise RuntimeError("未获取到视频流地址，无法下载")

        # ---- 仅下载音频（MP3）：直接取 Dash 音频流，转码即可，速度快且质量好 ----
        if capabilities.get("audio_only"):
            if not ffmpeg_available:
                raise RuntimeError("「仅下载音频」需要 FFmpeg，请先在 设置 中指定 FFmpeg 路径")
            if not audio_url:
                raise RuntimeError("未获取到音频流，无法仅下载音频")

            tmp_dir = tempfile.mkdtemp(prefix="bilibili_audio_")
            tmp_audio = os.path.join(tmp_dir, "audio.m4s")
            try:
                def audio_only_progress(downloaded, total):
                    if progress_callback and total:
                        progress_callback(int(downloaded * 0.9), int(total * 0.9))

                self._download_with_fallback(audio_candidates, headers, tmp_audio,
                                             audio_only_progress, cancel_event,
                                             connections, "音频流", adaptive)

                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("用户已取消下载")

                print("  正在转换为 MP3...")
                ffmpeg_extract_audio(ffmpeg_path, tmp_audio, save_path)
                if progress_callback:
                    progress_callback(100, 100)
                print(f"  下载完成: {save_path}")
            finally:
                if os.path.exists(tmp_audio):
                    os.remove(tmp_audio)
                if os.path.exists(tmp_dir):
                    os.rmdir(tmp_dir)
            return

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

                self._download_with_fallback(video_candidates, headers, tmp_video,
                                             video_progress, cancel_event,
                                             connections, "视频流", adaptive)

                # 下载音频流（进度 80-95%）
                def audio_progress(downloaded, total):
                    if progress_callback and total:
                        ratio = downloaded / total
                        progress_callback(int((0.8 + ratio * 0.15) * total), total)

                self._download_with_fallback(audio_candidates, headers, tmp_audio,
                                             audio_progress, cancel_event,
                                             connections, "音频流", adaptive)

                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("用户已取消下载")

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
            self._download_with_fallback(video_candidates, headers, save_path,
                                         progress_callback, cancel_event,
                                         connections, "视频流", adaptive)
            print(f"  下载完成: {save_path}")

    @staticmethod
    def _download_with_fallback(candidates, headers, dest, progress_callback,
                                cancel_event, connections, label,
                                adaptive: bool = True):
        """
        下载某个流（视频/音频）。

        两步走：
          1. **测速择优选址**：B站会返回多个 CDN（阿里/腾讯/华为/Akamai…），
             先各自采样测速，按实测速度从快到慢排序 —— 直接躲开"不报错但很慢"的 CDN；
          2. **依次尝试**：某个 CDN 被掐连接 / 超时时换下一个，避免整体失败。

        测速失败也没关系：无法测速的地址会保持原顺序排在后面，行为与优化前一致。
        """
        candidates = list(candidates or [])
        if len(candidates) > 1:
            candidates = probe_fastest_url(candidates, headers, cancel_event)

        last_error = None
        for index, url in enumerate(candidates, 1):
            host = urlparse(url).hostname or "?"
            if len(candidates) > 1:
                print(f"  开始下载{label}（CDN {index}/{len(candidates)}：{host}）")
            else:
                print(f"  开始下载{label}...")
            try:
                download_file(url, headers, dest,
                              progress_callback=progress_callback,
                              cancel_event=cancel_event,
                              connections=connections,
                              adaptive=adaptive)
                return
            except DownloadCancelled:
                raise
            except Exception as e:
                last_error = e
                print(f"  {label} CDN {index}（{host}）失败："
                      f"{type(e).__name__}: {str(e)[:120]}")
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"未获取到{label}的下载地址")

    # ---- 附加资源：弹幕 / 字幕 ----

    def download_extras(self, video_info: VideoInfo, save_path: str,
                        capabilities: dict, want_danmaku: bool = False,
                        want_subtitle: bool = False) -> list:
        """
        下载 B站弹幕与字幕（可选）。
        返回实际生成的文件路径列表；单项失败只记录日志，不影响主视频。
        """
        created: list[str] = []
        cid = video_info.get("cid")
        if not cid:
            return created

        base_dir = os.path.dirname(save_path) or "."
        base_name = os.path.splitext(os.path.basename(save_path))[0]
        headers = video_info.get("headers", dict(_DEFAULT_HEADERS))

        if want_danmaku:
            try:
                resp = requests.get(_DANMAKU_API.format(cid=cid), headers=headers, timeout=15)
                resp.raise_for_status()
                out = os.path.join(base_dir, f"{base_name}.danmaku.xml")
                with open(out, "wb") as f:
                    f.write(resp.content)
                created.append(out)
                print(f"  弹幕已保存: {out}")
            except Exception as e:
                print(f"  弹幕下载失败: {e}")

        if want_subtitle:
            try:
                created.extend(self._download_subtitles(video_info, base_dir, base_name, headers))
            except Exception as e:
                print(f"  字幕下载失败: {e}")

        return created

    def _download_subtitles(self, video_info: VideoInfo, base_dir: str,
                            base_name: str, headers: dict) -> list:
        """拉取字幕列表并逐个转成 SRT 保存"""
        bvid = video_info.get("bvid", "")
        cid = video_info.get("cid")
        if not bvid or not cid:
            return []

        resp = requests.get(_PLAYER_V2_API, params={"bvid": bvid, "cid": cid},
                            headers=headers, timeout=15)
        resp.raise_for_status()
        subtitles = ((resp.json().get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subtitles:
            print("  该视频没有可用字幕")
            return []

        created = []
        for i, sub in enumerate(subtitles):
            sub_url = sub.get("subtitle_url", "")
            if not sub_url:
                continue
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url

            detail = requests.get(sub_url, headers=headers, timeout=15).json()
            body = detail.get("body") or []
            if not body:
                continue

            lang = sub.get("lan_doc") or sub.get("lan") or f"sub{i + 1}"
            out = os.path.join(base_dir, f"{base_name}.{lang}.srt")
            with open(out, "w", encoding="utf-8") as f:
                f.write(_subtitle_to_srt(body))
            created.append(out)
            print(f"  字幕已保存: {out}")

        return created
