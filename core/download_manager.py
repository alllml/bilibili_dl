"""
下载协调层

【职责】GUI 与 Extractor 之间的中间层 ——
协调 URL 解析、视频信息提取、下载执行的完整流程，
使 GUI 层无需直接操作 Extractor 和 EnvironmentChecker 的细节。
"""
import os
import re
from datetime import datetime

from core.extractor_factory import ExtractorFactory
from core.environment_checker import EnvironmentChecker

# 文件名模板里允许的占位符（缺失的占位符会被替换成空串，而不是报错）
_TEMPLATE_FIELDS = ("title", "owner", "platform", "quality", "date")


class _SafeDict(dict):
    """format_map 用：缺失的占位符返回空串，避免模板写错直接抛异常"""

    def __missing__(self, key):
        return ""


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符与多余空白"""
    name = re.sub(r"[\s]+", " ", str(name))
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
    return name[:150]


class DownloadManager:
    """
    下载管理器。
    串联工厂 → 解析 → 下载的完整链路，向上层提供简洁的单一入口。
    """

    def __init__(self, factory: ExtractorFactory, env_checker: EnvironmentChecker):
        self._factory = factory
        self._env_checker = env_checker

    def start_download(self, url: str, save_dir: str,
                       progress_callback=None, cancel_event=None) -> str:
        """
        执行完整的下载流程（解析 + 下载）。
        返回最终保存的文件路径。
        """
        extractor = self._factory.get_extractor(url)
        if extractor is None:
            raise ValueError("不支持的链接，请输入 B 站或抖音的视频链接")

        cookie = ""
        if extractor.REQUIRES_COOKIE:
            cookie = self._env_checker.get_cookie_for_platform(extractor.PLATFORM_ID)

        video_info = extractor.extract_info(url, cookie=cookie)

        save_path = self._build_save_path(video_info, save_dir)

        capabilities = self._build_capabilities(cancel_event)
        extractor.download(video_info, save_path, capabilities,
                           progress_callback=progress_callback)

        return save_path

    def _build_capabilities(self, cancel_event=None) -> dict:
        """组装下发给插件的环境能力参数（含取消标记）"""
        capabilities = self._env_checker.get_capabilities()
        if cancel_event is not None:
            capabilities["cancel_event"] = cancel_event
        return capabilities

    def _build_save_path(self, video_info: dict, save_dir: str,
                         quality_name: str = "", audio_only: bool = False) -> str:
        """
        构建保存路径：
        - 按平台分子目录（bilibili/、douyin/ ...）
        - 文件名支持模板（设置 → 文件名模板），默认 {title}
        - 仅下载音频时扩展名为 .mp3
        """
        platform = video_info.get("platform", "other")

        template = self._env_checker.get_filename_template()
        fields = {
            "title": video_info.get("title", "video"),
            "owner": video_info.get("owner", ""),
            "platform": platform,
            "quality": quality_name or "",
            "date": datetime.now().strftime("%Y%m%d"),
        }
        try:
            rendered = template.format_map(_SafeDict(fields))
        except Exception:
            # 模板异常（如非法格式）时退回标题
            rendered = fields["title"]

        safe_name = _sanitize_filename(rendered) or "video"
        ext = ".mp3" if audio_only else ".mp4"

        platform_dir = os.path.join(save_dir, platform)
        os.makedirs(platform_dir, exist_ok=True)
        return self._deduplicate_path(os.path.join(platform_dir, f"{safe_name}{ext}"))

    @staticmethod
    def _deduplicate_path(path: str) -> str:
        """文件已存在时自动追加递增编号，如 title_2.mp4、title_3.mp4"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{base}_{n}{ext}"):
            n += 1
        return f"{base}_{n}{ext}"

    def start_download_with_info(self, video_info: dict, save_dir: str,
                                 quality_id: int = None,
                                 progress_callback=None,
                                 cancel_event=None,
                                 audio_only: bool = False,
                                 want_danmaku: bool = False,
                                 want_subtitle: bool = False) -> str:
        """
        使用已解析的 video_info 执行下载（跳过重复解析）。
        - quality_id: 指定清晰度
        - audio_only: 仅下载音频（MP3）
        - want_danmaku / want_subtitle: 是否同时下载弹幕 / 字幕（B站）
        """
        quality_name = ""

        # 按 quality_id 替换视频流 URL
        if quality_id is not None:
            streams = video_info.get("streams", [])
            selected = next(
                (s for s in streams
                 if s["type"] == "video" and s["quality_id"] == quality_id),
                None
            )
            if selected:
                video_info = dict(video_info)  # 浅拷贝，避免污染原数据
                # 保留覆盖前的原始地址（通常是音视频合一的 progressive 流），
                # 供插件在所选清晰度无音轨时作为兜底候选。
                video_info["primary_url"] = video_info.get("video_url", "")
                video_info["video_url"] = selected["url"]
                # 同步切换该清晰度的全部 CDN 候选（baseUrl + backupUrl）
                video_info["video_urls"] = selected.get("urls") or [selected["url"]]
                quality_name = selected.get("quality_name", "")

        save_path = self._build_save_path(video_info, save_dir, quality_name, audio_only)

        platform = video_info.get("platform", "")
        extractor = self._factory.get_extractor_by_platform(platform)
        if extractor is None:
            raise ValueError(f"未找到平台 {platform} 对应的下载器")

        capabilities = self._build_capabilities(cancel_event)
        if audio_only:
            capabilities["audio_only"] = True

        extractor.download(video_info, save_path, capabilities,
                           progress_callback=progress_callback)

        # 附加资源（弹幕 / 字幕）：失败不影响主视频，只记录日志
        if want_danmaku or want_subtitle:
            try:
                extractor.download_extras(
                    video_info, save_path, capabilities,
                    want_danmaku=want_danmaku, want_subtitle=want_subtitle)
            except Exception as e:
                print(f"[下载] 附加资源下载失败: {e}")

        return save_path
