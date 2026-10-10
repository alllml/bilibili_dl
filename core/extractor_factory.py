"""
工厂类 + 插件自动扫描 + URL 规范化

【设计模式】工厂模式 ——
ExtractorFactory 承担插件管理器的角色：
1. 通过 importlib 动态扫描 plugins/ 目录，自动发现 BaseExtractor 子类
2. 维护 域名 → 策略类 的注册表
3. 对用户输入的 URL 进行规范化（分享文案提取、短链还原、ID 补全、主域名提取）后匹配策略
"""
import importlib
import inspect
import os
import re
import sys
from typing import Dict, Optional, Type
from urllib.parse import urlparse

import requests

from core.base_extractor import BaseExtractor


# 需要进行短链/跳转还原的域名集合（含各平台官方短链服务）
_SHORT_LINK_DOMAINS = {
    "b23.tv",        # B 站官方短链
    "bili2233.cn",   # B 站另一短链域名
    "v.douyin.com",  # 抖音分享短链
    "m.douyin.com",  # 抖音移动端（可能继续跳转）
    "iesdouyin.com",  # 抖音分享落地页
    "t.co",          # Twitter 官方短链
}

# 从一大段文本（如分享文案）中提取第一个 URL。
# 字符集限定为 URL 合法字符，遇到空白、中文、中文标点、引号即自然截断。
_URL_IN_TEXT = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)

# 裸链接（用户省略 http:// 前缀，如 b23.tv/xxxx、www.douyin.com/video/123）
_BARE_DOMAIN = re.compile(
    r"^(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#]\S*)?$"
)

# B 站 BV / AV 号（可在纯文本或分享文案中被搜索到）
_BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
_AV_PATTERN = re.compile(r"(?<![0-9A-Za-z])av(\d+)", re.IGNORECASE)

# 抖音：只给了 modal_id=<id>（如从页面 URL 里复制的片段）
_DOUYIN_MODAL_ID = re.compile(r"^\s*modal_id\s*=\s*(\d{6,25})\s*$", re.IGNORECASE)
# 抖音：只给了一串纯数字 aweme_id
_BARE_NUMERIC_ID = re.compile(r"^\s*(\d{15,25})\s*$")

# 短链还原请求使用的 User-Agent
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 域名规范化时统一剥离的通用前缀
_STRIP_PREFIXES = ("www.", "m.", "mobile.", "wap.")


class ExtractorFactory:
    """
    【工厂模式】插件发现与注册中心 ——
    内部维护 Dict[域名, ExtractorClass] 注册表。
    新增平台插件时，只需将 .py 文件放入 plugins/ 目录，无需修改此类。
    """

    def __init__(self):
        # 注册表：域名 → 策略类
        self._registry: Dict[str, Type[BaseExtractor]] = {}

    def register(self, extractor_class: Type[BaseExtractor]) -> None:
        """
        将一个 Extractor 子类注册到工厂。
        读取子类的 SUPPORTED_DOMAINS，为每个域名建立映射。
        """
        for domain in extractor_class.SUPPORTED_DOMAINS:
            self._registry[domain.lower()] = extractor_class

    def scan_plugins(self) -> None:
        """
        【插件发现机制】
        利用 importlib 遍历 plugins/ 目录下的 .py 文件，
        动态加载模块并查找 BaseExtractor 的子类，完成自动注册。
        此过程在 GUI 启动初始化时调用，对用户透明。
        """
        # 定位 plugins 目录（兼容打包后的路径）
        if getattr(sys, 'frozen', False):
            # 打包模式：plugins/ 在 _internal/ 目录下（即 sys._MEIPASS 内）
            base_dir = sys._MEIPASS
            plugins_dir = os.path.join(base_dir, "plugins")
        else:
            # 开发模式：从 core/ 往上一级到项目根目录
            base_dir = os.path.dirname(os.path.abspath(__file__))
            plugins_dir = os.path.join(os.path.dirname(base_dir), "plugins")

        if not os.path.isdir(plugins_dir):
            print(f"[工厂] 插件目录不存在: {plugins_dir}")
            return

        for filename in os.listdir(plugins_dir):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            module_name = f"plugins.{filename[:-3]}"
            try:
                module = importlib.import_module(module_name)
            except Exception as e:
                print(f"[工厂] 加载插件 {filename} 失败: {e}")
                continue

            # 在模块中查找 BaseExtractor 的子类并注册
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseExtractor) and obj is not BaseExtractor:
                    self.register(obj)
                    print(f"[工厂] 已注册插件: {obj.__name__} -> {obj.SUPPORTED_DOMAINS}")

    # ---- URL 规范化 ----

    def normalize_url(self, url: str) -> str:
        """公开的 URL 规范化入口（供 GUI 等上层复用）"""
        return self._normalize_url(url)

    def _normalize_url(self, url: str) -> str:
        """
        URL 规范化（增强版）：
        1. 从整段分享文案中提取出第一个真正的 URL
        2. 裸链接补全 https:// 前缀（如 b23.tv/xxxx）
        3. 纯 BV / AV 号补全为完整 B 站链接
        4. 短链域名跟随 302 跳转还原最终地址
        """
        if not url:
            return url

        text = url.strip()

        # ---- 步骤 1：从分享文案中提取 URL ----
        match = _URL_IN_TEXT.search(text)
        if match:
            # 去掉可能被一并匹配到的尾部标点
            candidate = match.group(0).rstrip(".,;:!?)]}>\"'")
        else:
            candidate = text

        # ---- 非 URL 形态：可能是裸链接或纯 BV/AV 号 ----
        if not candidate.lower().startswith(("http://", "https://")):
            if _BARE_DOMAIN.match(candidate):
                # 步骤 2：裸链接补全协议
                candidate = "https://" + candidate
            else:
                # 步骤 3：纯 BV / AV 号补全
                bv = _BV_PATTERN.search(text)
                if bv:
                    full_url = f"https://www.bilibili.com/video/{bv.group(0)}"
                    print(f"[工厂] BV 号补全: {bv.group(0)} -> {full_url}")
                    return full_url

                av = _AV_PATTERN.search(text)
                if av:
                    full_url = f"https://www.bilibili.com/video/av{av.group(1)}"
                    print(f"[工厂] AV 号补全: av{av.group(1)} -> {full_url}")
                    return full_url

                # 步骤 3.5：抖音 modal_id=<id> 片段，或一串纯数字 aweme_id
                modal = _DOUYIN_MODAL_ID.match(text)
                if modal:
                    # 保留精选页形态：实测该页面的 SSR 数据里带完整 videoDetail，
                    # 而 /video/{id} 页面反而拿不到详情。
                    full_url = f"https://www.douyin.com/jingxuan?modal_id={modal.group(1)}"
                    print(f"[工厂] 抖音 modal_id 补全: {modal.group(1)} -> {full_url}")
                    return full_url

                bare = _BARE_NUMERIC_ID.match(text)
                if bare:
                    full_url = f"https://www.douyin.com/video/{bare.group(1)}"
                    print(f"[工厂] 抖音作品 ID 补全: {bare.group(1)} -> {full_url}")
                    return full_url

                # 无法识别，原样返回，由上层提示“不支持的链接”
                return text

        # ---- 步骤 4：短链还原 ----
        hostname = (urlparse(candidate).hostname or "").lower()
        if any(hostname == d or hostname.endswith("." + d)
               for d in _SHORT_LINK_DOMAINS):
            try:
                resp = requests.head(candidate, allow_redirects=True,
                                     timeout=10, headers={"User-Agent": _UA})
                if resp.url and resp.url != candidate:
                    print(f"[工厂] 短链还原: {candidate} -> {resp.url}")
                    candidate = resp.url
            except requests.RequestException as e:
                print(f"[工厂] 短链还原失败: {e}")

        return candidate

    def _extract_domain(self, url: str) -> str:
        """从 URL 中提取主域名（剥离 www./m./mobile./wap. 等通用前缀，统一小写）"""
        hostname = (urlparse(url).hostname or "").lower()
        for prefix in _STRIP_PREFIXES:
            if hostname.startswith(prefix):
                hostname = hostname[len(prefix):]
                break
        return hostname

    def _lookup(self, domain: str) -> Optional[Type[BaseExtractor]]:
        """
        按域名查找策略类。
        先精确匹配；再按后缀匹配，使任意子域名（如 live.douyin.com、
        www.iesdouyin.com）都能命中已注册的主域名。
        """
        cls = self._registry.get(domain)
        if cls is not None:
            return cls

        for reg_domain, reg_cls in self._registry.items():
            if domain == reg_domain or domain.endswith("." + reg_domain):
                return reg_cls
        return None

    def get_extractor(self, url: str) -> Optional[BaseExtractor]:
        """
        根据 URL 获取对应的策略实例：
        1. 规范化 URL（文案提取、短链还原、ID 补全）
        2. 提取主域名
        3. 从注册表中查找并实例化对应的 Extractor
        """
        normalized_url = self._normalize_url(url)
        domain = self._extract_domain(normalized_url)

        extractor_class = self._lookup(domain)
        if extractor_class is None:
            print(f"[工厂] 不支持的链接: {domain}")
            return None

        return extractor_class()

    def is_supported_text(self, text: str) -> bool:
        """
        轻量判断一段文本里是否包含本工具支持的视频链接。
        纯离线判断（不做短链网络还原），用于剪贴板监听 / 拖拽时的即时提示。
        """
        if not text:
            return False

        # 文本中存在 http(s) 链接
        match = _URL_IN_TEXT.search(text)
        if match:
            candidate = match.group(0).rstrip(".,;:!?)]}>\"'")
            return self._lookup(self._extract_domain(candidate)) is not None

        # 纯 BV / AV 号
        if _BV_PATTERN.search(text) or _AV_PATTERN.search(text):
            return True

        # 抖音：modal_id=<id> 片段，或一串纯数字作品 ID
        if _DOUYIN_MODAL_ID.match(text) or _BARE_NUMERIC_ID.match(text):
            return True

        # 裸域名（如 b23.tv/xxxx）
        stripped = text.strip()
        if _BARE_DOMAIN.match(stripped):
            return self._lookup(self._extract_domain("https://" + stripped)) is not None

        return False

    def get_registered_platforms(self) -> list[dict]:
        """
        返回所有已注册插件的平台元数据（去重）。
        供 GUI 动态生成菜单、状态栏等使用，无需硬编码平台列表。
        返回格式：[{"platform_id": "bilibili", "platform_name": "B站", "requires_cookie": True}, ...]
        """
        seen = set()
        platforms = []
        for cls in self._registry.values():
            pid = cls.PLATFORM_ID
            if not pid or pid in seen:
                continue
            seen.add(pid)
            platforms.append({
                "platform_id": pid,
                "platform_name": cls.PLATFORM_NAME or pid,
                "requires_cookie": cls.REQUIRES_COOKIE,
            })
        return platforms

    def get_extractor_by_platform(self, platform: str) -> Optional[BaseExtractor]:
        """
        按平台标识获取策略实例（供 DownloadManager 在已有 video_info 时使用）。
        遍历注册表，查找 SUPPORTED_DOMAINS 中第一个匹配平台名的 Extractor 类。
        """
        for domain, cls in self._registry.items():
            if platform in domain:
                return cls()
        return None
