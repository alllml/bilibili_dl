"""
工厂类 + 插件自动扫描 + URL 规范化

【设计模式】工厂模式 ——
ExtractorFactory 承担插件管理器的角色：
1. 通过 importlib 动态扫描 plugins/ 目录，自动发现 BaseExtractor 子类
2. 维护 域名 → 策略类 的注册表
3. 对用户输入的 URL 进行规范化（短链还原、主域名提取）后匹配策略
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


# 需要进行短链还原的域名集合
_SHORT_LINK_DOMAINS = {"b23.tv", "v.douyin.com", "m.douyin.com", "t.co"}

# 纯 BV/AV 号正则（无域名时匹配）
_BV_PATTERN = re.compile(r"^BV[a-zA-Z0-9]{10}$", re.IGNORECASE)
_AV_PATTERN = re.compile(r"^av\d+$", re.IGNORECASE)


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
            self._registry[domain] = extractor_class

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

    def _normalize_url(self, url: str) -> str:
        """
        URL 规范化处理：
        1. 纯 BV/AV 号补全为完整 URL
        2. 对短链域名（b23.tv、v.douyin.com）发起 HEAD 请求，跟随 302 跳转获取最终 URL
        3. 其他域名直接返回，不发网络请求
        """
        url = url.strip()

        # 纯 BV 号：补全为完整 URL
        if _BV_PATTERN.match(url):
            full_url = f"https://www.bilibili.com/video/{url}"
            print(f"[工厂] BV 号补全: {url} -> {full_url}")
            return full_url

        # 纯 AV 号：补全为完整 URL
        if _AV_PATTERN.match(url):
            full_url = f"https://www.bilibili.com/video/{url}"
            print(f"[工厂] AV 号补全: {url} -> {full_url}")
            return full_url

        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        if hostname in _SHORT_LINK_DOMAINS:
            try:
                resp = requests.head(url, allow_redirects=True, timeout=10)
                url = resp.url
                print(f"[工厂] 短链还原: {parsed.geturl()} -> {url}")
            except requests.RequestException as e:
                print(f"[工厂] 短链还原失败: {e}")

        return url

    def _extract_domain(self, url: str) -> str:
        """从 URL 中提取主域名（去掉 www. 等前缀）"""
        hostname = urlparse(url).hostname or ""
        # 去掉 www. 和 m. 前缀
        if hostname.startswith("www."):
            hostname = hostname[4:]
        elif hostname.startswith("m."):
            hostname = hostname[2:]
        return hostname

    def get_extractor(self, url: str) -> Optional[BaseExtractor]:
        """
        根据 URL 获取对应的策略实例：
        1. 规范化 URL（短链还原）
        2. 提取主域名
        3. 从注册表中查找并实例化对应的 Extractor
        """
        normalized_url = self._normalize_url(url)
        domain = self._extract_domain(normalized_url)

        extractor_class = self._registry.get(domain)
        if extractor_class is None:
            print(f"[工厂] 不支持的链接: {domain}")
            return None

        return extractor_class()

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
