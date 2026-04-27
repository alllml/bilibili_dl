"""
B 站 Cookie 管理器 - Selenium 模拟登录

职责：
1. 自动检测本地浏览器（Edge → Chrome）
2. 打开 B 站登录页，等待用户扫码或账密登录
3. 检测到 SESSDATA Cookie 后自动关闭浏览器
4. 返回 Cookie 字符串供上层保存
"""
import time
from selenium import webdriver
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import WebDriverException


class BilibiliCookieManager:
    """B 站 Cookie 获取管理器（基于 Selenium 模拟登录）"""

    BILIBILI_LOGIN_URL = "https://passport.bilibili.com/login"
    CHECK_INTERVAL = 1  # 每秒检查一次 Cookie
    MAX_WAIT_TIME = 300  # 最多等待 5 分钟

    def __init__(self):
        self._driver = None

    def get_cookie_by_login(self) -> str | None:
        """
        打开浏览器让用户登录 B 站，获取 SESSDATA Cookie。
        返回 Cookie 字符串（格式：SESSDATA=xxx），失败返回 None。
        """
        # 尝试启动浏览器
        if not self._start_browser():
            return None

        try:
            # 打开 B 站登录页
            self._driver.get(self.BILIBILI_LOGIN_URL)
            print("[Cookie 管理器] 已打开 B 站登录页，请在浏览器中完成登录...")

            # 轮询检测 SESSDATA Cookie
            start_time = time.time()
            while time.time() - start_time < self.MAX_WAIT_TIME:
                sessdata = self._get_sessdata()
                if sessdata:
                    print(f"[Cookie 管理器] 检测到登录成功，SESSDATA: {sessdata[:20]}...")
                    return f"SESSDATA={sessdata}"
                time.sleep(self.CHECK_INTERVAL)

            print("[Cookie 管理器] 超时未检测到登录，已取消")
            return None

        except Exception as e:
            print(f"[Cookie 管理器] 获取 Cookie 失败: {e}")
            return None
        finally:
            self._close_browser()

    def _start_browser(self) -> bool:
        """
        按优先级启动浏览器：Edge → Chrome。
        返回是否成功启动。
        """
        # 尝试 Edge
        try:
            options = webdriver.EdgeOptions()
            # 不使用代理（B 站是国内网站）
            # 不加载图片，加快速度
            options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
            self._driver = webdriver.Edge(options=options)
            print("[Cookie 管理器] 使用 Edge 浏览器")
            return True
        except WebDriverException as e:
            print(f"[Cookie 管理器] Edge 启动失败: {e}")

        # 尝试 Chrome
        try:
            options = webdriver.ChromeOptions()
            options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
            self._driver = webdriver.Chrome(options=options)
            print("[Cookie 管理器] 使用 Chrome 浏览器")
            return True
        except WebDriverException as e:
            print(f"[Cookie 管理器] Chrome 启动失败: {e}")

        print("[Cookie 管理器] 未找到可用的浏览器（需要 Edge 或 Chrome）")
        return False

    def _get_sessdata(self) -> str | None:
        """从当前浏览器会话中获取 SESSDATA Cookie"""
        try:
            cookies = self._driver.get_cookies()
            for cookie in cookies:
                if cookie.get("name") == "SESSDATA":
                    return cookie.get("value")
        except Exception:
            pass
        return None

    def _close_browser(self):
        """关闭浏览器"""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


class DouyinCookieManager:
    """抖音 Cookie 获取管理器（基于 Selenium 模拟登录）"""

    DOUYIN_LOGIN_URL = "https://www.douyin.com"
    CHECK_INTERVAL = 1  # 每秒检查一次 Cookie
    MAX_WAIT_TIME = 300  # 最多等待 5 分钟

    def __init__(self):
        self._driver = None

    def get_cookie_by_login(self) -> str | None:
        """
        打开浏览器让用户登录抖音，获取完整 Cookie。
        返回 Cookie 字符串，失败返回 None。
        """
        if not self._start_browser():
            return None

        try:
            # 打开抖音首页
            self._driver.get(self.DOUYIN_LOGIN_URL)
            print("[Cookie 管理器] 已打开抖音首页，请点击登录按钮并完成扫码...")

            # 轮询检测关键 Cookie（sessionid 和 ttwid）
            start_time = time.time()
            while time.time() - start_time < self.MAX_WAIT_TIME:
                cookie_str = self._get_full_cookie()
                if cookie_str and "sessionid" in cookie_str and "ttwid" in cookie_str:
                    print(f"[Cookie 管理器] 检测到登录成功，Cookie 长度: {len(cookie_str)}")
                    return cookie_str
                time.sleep(self.CHECK_INTERVAL)

            print("[Cookie 管理器] 超时未检测到登录，已取消")
            return None

        except Exception as e:
            print(f"[Cookie 管理器] 获取 Cookie 失败: {e}")
            return None
        finally:
            self._close_browser()

    def _start_browser(self) -> bool:
        """
        按优先级启动浏览器：Edge → Chrome。
        不禁用图片加载（抖音扫码登录需要显示二维码图片）。
        返回是否成功启动。
        """
        # 尝试 Edge
        try:
            options = webdriver.EdgeOptions()
            self._driver = webdriver.Edge(options=options)
            print("[Cookie 管理器] 使用 Edge 浏览器")
            return True
        except WebDriverException as e:
            print(f"[Cookie 管理器] Edge 启动失败: {e}")

        # 尝试 Chrome
        try:
            options = webdriver.ChromeOptions()
            self._driver = webdriver.Chrome(options=options)
            print("[Cookie 管理器] 使用 Chrome 浏览器")
            return True
        except WebDriverException as e:
            print(f"[Cookie 管理器] Chrome 启动失败: {e}")

        print("[Cookie 管理器] 未找到可用的浏览器（需要 Edge 或 Chrome）")
        return False

    def _get_full_cookie(self) -> str | None:
        """
        从当前浏览器会话中获取关键 Cookie 字段。
        只提取登录态必需的核心字段，避免 Cookie 过长难以管理。
        """
        try:
            cookies = self._driver.get_cookies()
            if not cookies:
                return None

            # 关键字段列表（登录态必需）
            key_fields = [
                "sessionid",      # 登录会话标识（核心）
                "ttwid",          # 设备指纹（核心）
                "sid_guard",      # 会话守卫（可选，增强稳定性）
                "odin_tt",        # 设备标识（可选）
            ]

            # 只提取关键字段
            cookie_dict = {c['name']: c['value'] for c in cookies if c.get('name')}
            filtered = [f"{k}={cookie_dict[k]}" for k in key_fields if k in cookie_dict]

            if not filtered:
                return None

            cookie_str = "; ".join(filtered)
            print(f"[Cookie 管理器] 已精简 Cookie，保留 {len(filtered)} 个关键字段（总长度: {len(cookie_str)} 字符）")
            return cookie_str
        except Exception:
            pass
        return None

    def _close_browser(self):
        """关闭浏览器"""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
