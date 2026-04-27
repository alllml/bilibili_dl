"""
抽象策略接口 + VideoInfo 数据结构定义

【设计模式】策略模式的接口层 ——
所有视频平台插件必须继承 BaseExtractor 并实现其抽象方法。
GUI / DownloadManager 只依赖此抽象接口，不感知具体平台实现。
"""
from abc import ABC, abstractmethod
from typing import TypedDict


class VideoInfo(TypedDict, total=False):
    """
    视频元数据的标准化结构，由 extract_info() 返回。
    所有平台插件统一输出此格式，下游消费者（DownloadManager / download）无需关心来源平台。
    """
    title: str              # 视频标题
    cover_url: str          # 封面图 URL
    video_url: str          # 视频流 URL（降级模式下的单一流）
    audio_url: str          # 音频流 URL（Dash 模式）
    streams: list           # 可用的清晰度列表
    headers: dict           # 下载时需要携带的请求头（Referer、Cookie 等）
    platform: str           # 来源平台标识（如 "bilibili"、"douyin"）
    duration: int           # 视频时长（秒）
    owner: str              # UP 主名称
    view_count: int         # 播放量
    danmaku_count: int      # 弹幕数


class BaseExtractor(ABC):
    """
    【策略模式】抽象策略基类 ——
    定义所有视频平台插件必须实现的统一行为契约。
    工厂类通过 SUPPORTED_DOMAINS 自动发现并注册子类。
    """

    # 子类必须声明自己支持的域名列表，供工厂注册时使用
    SUPPORTED_DOMAINS: list[str] = []

    # 平台标识（用于 Cookie 管理、配置存储等）
    PLATFORM_ID: str = ""

    # 平台显示名称（用于 GUI 显示）
    PLATFORM_NAME: str = ""

    # 是否需要 Cookie（True 时 GUI 会生成对应的 Cookie 设置菜单）
    REQUIRES_COOKIE: bool = False

    @abstractmethod
    def extract_info(self, url: str, cookie: str = "") -> VideoInfo:
        """
        解析视频页面，返回标准化的 VideoInfo 字典。
        此方法负责网络请求与数据提取，不涉及文件下载。
        cookie 参数可选，由上层（DownloadManager / GUI）从配置中读取后传入。
        """
        ...

    @abstractmethod
    def download(self, video_info: VideoInfo, save_path: str, capabilities: dict,
                 progress_callback=None) -> None:
        """
        根据 extract_info 的结果执行下载编排。
        - video_info: extract_info() 的返回值
        - save_path: 文件保存路径
        - capabilities: 环境能力参数（如 ffmpeg_available: bool, ffmpeg_path: str）
        - progress_callback: 进度回调，签名 callback(downloaded, total)
        实际的 HTTP 分块下载委托给公共工具函数 download_file。
        """
        ...
