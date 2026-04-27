"""
FFmpeg 环境检测与配置管理

职责：
1. 检测 FFmpeg 是否可用（自定义路径优先 → 系统 PATH）
2. 读写 config.ini 中的用户配置（FFmpeg 路径、B 站 Cookie）
3. 向上层提供 capabilities 信息，供策略类做降级决策
"""
import configparser
import os
import subprocess
import sys


class EnvironmentChecker:
    """
    环境检测与配置管理器。
    config.ini 格式：
    [settings]
    ffmpeg_path =
    bilibili_cookie =
    """

    def __init__(self, config_path: str = "config.ini"):
        # 打包环境下，config.ini 放在 exe 同级目录
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
            config_path = os.path.join(base_dir, config_path)

        self._config_path = config_path
        self._config = configparser.RawConfigParser()
        self._ffmpeg_path: str | None = None

        # 确保配置文件和 [settings] 节存在
        if os.path.exists(config_path):
            self._config.read(config_path, encoding="utf-8")
        if not self._config.has_section("settings"):
            self._config.add_section("settings")

        # 首次启动时自动生成默认 config.ini，方便用户编辑
        if not os.path.exists(config_path):
            self._config.set("settings", "ffmpeg_path", "")
            self._config.set("settings", "bilibili_cookie", "")
            self._config.set("settings", "douyin_cookie", "")
            self._config.set("settings", "twitter_cookie", "")
            self._config.set("settings", "save_dir", "")
            self._save_config()

    def _save_config(self) -> None:
        """将当前配置写入 config.ini"""
        with open(self._config_path, "w", encoding="utf-8") as f:
            self._config.write(f)

    # ---- FFmpeg 检测 ----

    def check_ffmpeg(self) -> str | None:
        """
        按优先级检测 FFmpeg 可用性：
        1. config.ini 中用户指定的自定义路径
        2. 系统 PATH 中的 ffmpeg
        返回可用的 ffmpeg 路径，不可用返回 None。
        """
        # 优先级 1：用户自定义路径
        custom_path = self._config.get("settings", "ffmpeg_path", fallback="").strip()
        if custom_path and self._test_ffmpeg(custom_path):
            self._ffmpeg_path = custom_path
            return self._ffmpeg_path

        # 优先级 2：系统 PATH
        if self._test_ffmpeg("ffmpeg"):
            self._ffmpeg_path = "ffmpeg"
            # 自动记录到 config.ini（如果配置为空）
            if not custom_path:
                # 获取 ffmpeg 的完整路径
                import shutil
                full_path = shutil.which("ffmpeg")
                if full_path:
                    self._config.set("settings", "ffmpeg_path", full_path)
                    self._save_config()
            return self._ffmpeg_path

        self._ffmpeg_path = None
        return None

    def _test_ffmpeg(self, path: str) -> bool:
        """测试指定路径的 ffmpeg 是否可执行"""
        try:
            result = subprocess.run(
                [path, "-version"],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def get_ffmpeg_path(self) -> str | None:
        """返回缓存的 FFmpeg 路径（需先调用 check_ffmpeg）"""
        return self._ffmpeg_path

    def set_ffmpeg_path(self, path: str) -> None:
        """保存用户指定的 FFmpeg 路径到 config.ini 并更新缓存"""
        self._config.set("settings", "ffmpeg_path", path)
        self._save_config()
        # 重新检测
        self.check_ffmpeg()

    # ---- Cookie 管理（统一接口）----

    def get_cookie_for_platform(self, platform_id: str) -> str:
        """
        统一接口：根据平台 ID 获取 Cookie。
        平台 ID 由插件的 PLATFORM_ID 属性定义（如 "bilibili", "douyin", "twitter"）。
        """
        key = f"{platform_id}_cookie"
        return self._config.get("settings", key, fallback="").strip()

    def set_cookie_for_platform(self, platform_id: str, cookie: str) -> None:
        """
        统一接口：保存平台 Cookie。
        抖音 Cookie 会自动精简，只保留关键字段。
        """
        cookie = cookie.strip()
        key = f"{platform_id}_cookie"

        # 抖音 Cookie 特殊处理：只保留关键字段
        if platform_id == "douyin" and cookie:
            key_fields = ["sessionid", "ttwid", "sid_guard", "odin_tt"]
            cookie_dict = {}
            for item in cookie.split(";"):
                item = item.strip()
                if "=" in item:
                    name, value = item.split("=", 1)
                    name = name.strip()
                    if name in key_fields:
                        cookie_dict[name] = value.strip()

            if cookie_dict:
                cookie = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                print(f"[环境检测] 抖音 Cookie 已精简，保留 {len(cookie_dict)} 个关键字段")
            else:
                print(f"[环境检测] 未检测到标准关键字段，保存原始 Cookie")

        self._config.set("settings", key, cookie)
        self._save_config()

    # ---- 兼容旧代码的别名方法（逐步废弃）----

    def get_cookie(self) -> str:
        """读取 B 站 Cookie（兼容旧代码）"""
        return self.get_cookie_for_platform("bilibili")

    def set_cookie(self, cookie: str) -> None:
        """保存 B 站 Cookie（兼容旧代码）"""
        self.set_cookie_for_platform("bilibili", cookie)

    def get_douyin_cookie(self) -> str:
        """读取抖音 Cookie（兼容旧代码）"""
        return self.get_cookie_for_platform("douyin")

    def set_douyin_cookie(self, cookie: str) -> None:
        """保存抖音 Cookie（兼容旧代码）"""
        self.set_cookie_for_platform("douyin", cookie)

    def get_twitter_cookie(self) -> str:
        """读取 Twitter Cookie（兼容旧代码）"""
        return self.get_cookie_for_platform("twitter")

    def set_twitter_cookie(self, cookie: str) -> None:
        """保存 Twitter Cookie（兼容旧代码）"""
        self.set_cookie_for_platform("twitter", cookie)

    # ---- 存储目录管理 ----

    def get_save_dir(self) -> str:
        """读取 config.ini 中保存的存储目录"""
        return self._config.get("settings", "save_dir", fallback="").strip()

    def set_save_dir(self, path: str) -> None:
        """保存存储目录到 config.ini"""
        self._config.set("settings", "save_dir", path.strip())
        self._save_config()

    # ---- 能力参数 ----

    def get_capabilities(self) -> dict:
        """返回环境能力参数字典，供策略类的 download() 方法使用"""
        return {
            "ffmpeg_available": self._ffmpeg_path is not None,
            "ffmpeg_path": self._ffmpeg_path or "",
        }

    # ---- 并发下载数 ----

    def get_max_concurrent(self) -> int:
        """读取最大并发下载数，默认为 2"""
        val = self._config.get("settings", "max_concurrent", fallback="2").strip()
        try:
            return max(1, min(5, int(val)))
        except ValueError:
            return 2

    def set_max_concurrent(self, n: int) -> None:
        """保存最大并发下载数到 config.ini"""
        self._config.set("settings", "max_concurrent", str(max(1, min(5, n))))
        self._save_config()
