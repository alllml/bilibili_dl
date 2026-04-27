"""
下载协调层

【职责】GUI 与 Extractor 之间的中间层 ——
协调 URL 解析、视频信息提取、下载执行的完整流程，
使 GUI 层无需直接操作 Extractor 和 EnvironmentChecker 的细节。
"""
import os
import re

from core.extractor_factory import ExtractorFactory
from core.environment_checker import EnvironmentChecker


class DownloadManager:
    """
    下载管理器。
    串联工厂 → 解析 → 下载的完整链路，向上层提供简洁的单一入口。
    """

    def __init__(self, factory: ExtractorFactory, env_checker: EnvironmentChecker):
        self._factory = factory
        self._env_checker = env_checker

    def start_download(self, url: str, save_dir: str,
                       progress_callback=None) -> str:
        """
        执行完整的下载流程：
        1. 通过工厂获取对应平台的 Extractor
        2. 调用 extract_info 获取视频元数据
        3. 调用 download 执行下载

        参数：
            url: 视频链接
            save_dir: 保存目录
            progress_callback: 进度回调，签名 callback(downloaded, total)

        返回：最终保存的文件路径
        """
        # 获取策略实例
        extractor = self._factory.get_extractor(url)
        if extractor is None:
            raise ValueError("不支持的链接，请输入 B 站或抖音的视频链接")

        # 根据插件的 PLATFORM_ID 自动获取对应的 Cookie
        cookie = ""
        if extractor.REQUIRES_COOKIE:
            cookie = self._env_checker.get_cookie_for_platform(extractor.PLATFORM_ID)

        # 解析视频信息
        video_info = extractor.extract_info(url, cookie=cookie)

        title = video_info.get("title", "video")
        # 清理文件名中的非法字符
        safe_title = re.sub(r'[\s]+', ' ', title)
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', safe_title).strip() or "video"
        safe_title = safe_title[:120]

        # 按平台分子目录存储（bilibili/、douyin/ 等）
        platform = video_info.get("platform", "other")
        platform_dir = os.path.join(save_dir, platform)
        os.makedirs(platform_dir, exist_ok=True)
        save_path = os.path.join(platform_dir, f"{safe_title}.mp4")

        # 重名文件自动递增编号，避免覆盖
        save_path = self._deduplicate_path(save_path)

        # 获取环境能力参数并执行下载
        capabilities = self._env_checker.get_capabilities()
        extractor.download(video_info, save_path, capabilities,
                           progress_callback=progress_callback)

        return save_path

    def _build_save_path(self, video_info: dict, save_dir: str,
                         quality_name: str = "") -> str:
        """构建分平台存储路径，可选附加清晰度后缀避免文件名冲突"""
        title = video_info.get("title", "video")
        safe_title = re.sub(r'[\s]+', ' ', title)
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', safe_title).strip() or "video"
        safe_title = safe_title[:120]

        platform = video_info.get("platform", "other")
        platform_dir = os.path.join(save_dir, platform)
        os.makedirs(platform_dir, exist_ok=True)

        suffix = f"_{quality_name}" if quality_name else ""
        save_path = os.path.join(platform_dir, f"{safe_title}{suffix}.mp4")
        return self._deduplicate_path(save_path)

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
                                 progress_callback=None) -> str:
        """
        使用已解析的 video_info 执行下载（跳过重复解析）。
        支持按 quality_id 选择指定清晰度的视频流。
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
                video_info["video_url"] = selected["url"]
                quality_name = selected.get("quality_name", "")

        save_path = self._build_save_path(video_info, save_dir, quality_name)

        # 获取对应平台的 Extractor 实例
        platform = video_info.get("platform", "")
        extractor = self._factory.get_extractor_by_platform(platform)
        if extractor is None:
            raise ValueError(f"未找到平台 {platform} 对应的下载器")

        capabilities = self._env_checker.get_capabilities()
        extractor.download(video_info, save_path, capabilities,
                           progress_callback=progress_callback)

        return save_path
