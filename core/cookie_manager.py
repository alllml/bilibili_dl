"""
B站 / 抖音 Cookie 管理器 - Selenium 模拟登录

【本轮针对「扫码登录太慢」的优化】
1. 持久化用户数据目录（--user-data-dir）：首次登录后登录态保留在本地，
   下次打开浏览器通常「已登录」，直接读 Cookie，免去再次扫码；
2. 反自动化检测参数（隐藏 webdriver 特征等）：降低被平台判定为机器人
   而导致扫码成功却拿不到登录态的概率，提升准确度；
3. 轮询间隔 1s → 0.4s，且判据只看「登录态关键字段」：
   抖音只看 sessionid（ttwid 未登录时就存在，旧条件等于白等）；
4. 打开页面后尽力自动点击「登录」入口并切到「扫码登录」，
   省去用户自己找按钮——这是人工最耗时间的一步；
5. 全程通过 status_callback 汇报进度（含已等待秒数），并支持中途取消。
"""
import os
import tempfile
import time

from selenium import webdriver
from selenium.common.exceptions import WebDriverException


# 反自动化 + 稳定性参数（与抖音解析插件保持一致）
_ANTI_DETECT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--start-maximized",
]

# 尽力自动点击「登录」入口（精确文本匹配，避免误点；失败不影响手动操作）
_AUTO_LOGIN_SCRIPT = """
var targets = ['登录', '立即登录', '登录/注册'];
var nodes = document.querySelectorAll('div,button,span,a,p');
for (var i = 0; i < nodes.length; i++) {
    var t = (nodes[i].textContent || '').trim();
    if (targets.indexOf(t) > -1 && nodes[i].offsetParent !== null) {
        nodes[i].click();
        return true;
    }
}
return false;
"""

# 自动切到「扫码登录」标签
_AUTO_SCAN_TAB_SCRIPT = """
var targets = ['扫码登录', '二维码登录', '扫码'];
var nodes = document.querySelectorAll('div,span,li,a,p,button');
for (var i = 0; i < nodes.length; i++) {
    var t = (nodes[i].textContent || '').trim();
    if (targets.indexOf(t) > -1 && nodes[i].offsetParent !== null) {
        nodes[i].click();
        return true;
    }
}
return false;
"""


def _profile_dir(platform_id: str) -> str:
    """
    持久化浏览器用户数据目录。
    放在用户本地目录（Windows 为 %LOCALAPPDATA%），不污染项目/打包目录。
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "VideoDownloader", "browser_profiles", platform_id)
    os.makedirs(path, exist_ok=True)
    return path


def _start_browser(platform_id: str):
    """
    启动浏览器（优先 Edge，其次 Chrome）：
    - 使用持久化 profile，保留登录态
    - 注入反自动化参数，避免被平台识别
    返回 WebDriver 实例，失败返回 None。
    """
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    profile = _profile_dir(platform_id)

    def _build(options, driver_cls):
        for arg in _ANTI_DETECT_ARGS:
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={profile}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        driver = driver_cls(options=options)
        # 隐藏 navigator.webdriver 特征
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'})
        return driver

    try:
        driver = _build(EdgeOptions(), webdriver.Edge)
        print("[Cookie 管理器] 使用 Edge 浏览器")
        return driver
    except WebDriverException as e:
        print(f"[Cookie 管理器] Edge 启动失败: {e}")

    try:
        driver = _build(ChromeOptions(), webdriver.Chrome)
        print("[Cookie 管理器] 使用 Chrome 浏览器")
        return driver
    except WebDriverException as e:
        print(f"[Cookie 管理器] Chrome 启动失败: {e}")

    return None


class _SeleniumLoginBase:
    """Selenium 登录管理器的公共骨架：启动 → 打开 → 自动引导 → 轮询 → 取 Cookie。"""

    LOGIN_URL = ""
    PLATFORM_ID = ""
    PLATFORM_NAME = ""
    CHECK_INTERVAL = 0.4   # 轮询间隔（秒）
    MAX_WAIT_TIME = 300    # 最长等待时间（秒）

    def __init__(self):
        self._driver = None
        self._cancelled = False

    # ---- 子类需实现 ----
    def _is_logged_in(self) -> bool:
        """判断是否已处于登录态（只看登录态关键 Cookie）"""
        raise NotImplementedError

    def _build_cookie(self) -> str:
        """从浏览器读取并组装 Cookie 字符串"""
        raise NotImplementedError

    def _after_open(self, driver) -> None:
        """打开页面后的自动引导（可选）"""
        pass

    # ---- 公共逻辑 ----

    def _get_cookie_value(self, name: str):
        """读取指定名称的 Cookie 值（不存在返回 None）"""
        try:
            for cookie in self._driver.get_cookies():
                if cookie.get("name") == name:
                    return cookie.get("value")
        except Exception:
            pass
        return None

    def cancel(self):
        """请求取消登录等待（随时可调用）"""
        self._cancelled = True

    def get_cookie_by_login(self, status_callback=None) -> str | None:
        """
        打开浏览器让用户完成登录，返回 Cookie 字符串；失败或取消返回 None。
        status_callback: 可选，接收进度文本（用于界面实时提示）。
        """

        def report(msg: str):
            print(f"[Cookie 管理器] {msg}")
            if status_callback:
                try:
                    status_callback(msg)
                except Exception:
                    pass

        self._cancelled = False
        report("正在启动浏览器…")
        self._driver = _start_browser(self.PLATFORM_ID)
        if self._driver is None:
            report("未找到可用的浏览器（需要安装 Edge 或 Chrome）")
            return None

        try:
            report(f"正在打开{self.PLATFORM_NAME}登录页…")
            self._driver.get(self.LOGIN_URL)
            time.sleep(1.5)

            # 持久化 profile 生效时，通常已处于登录态 → 直接返回，省去扫码
            if self._is_logged_in():
                report("检测到已登录状态，直接读取 Cookie")
                return self._build_cookie()

            # 尽力自动打开「登录 → 扫码」
            self._after_open(self._driver)

            report(f"请在浏览器中扫码登录{self.PLATFORM_NAME}")
            start = time.time()
            last_notice = -1
            while time.time() - start < self.MAX_WAIT_TIME:
                if self._cancelled:
                    report("已取消")
                    return None
                if self._is_logged_in():
                    report("检测到登录成功，正在读取 Cookie…")
                    return self._build_cookie()

                elapsed = int(time.time() - start)
                if elapsed and elapsed % 10 == 0 and elapsed != last_notice:
                    last_notice = elapsed
                    report(f"仍在等待扫码…已等待 {elapsed} 秒")
                time.sleep(self.CHECK_INTERVAL)

            report("等待超时，已取消")
            return None

        except Exception as e:
            report(f"获取 Cookie 失败: {e}")
            return None
        finally:
            self._close_browser()

    def _close_browser(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


class BilibiliCookieManager(_SeleniumLoginBase):
    """B 站 Cookie 获取管理器（Selenium 模拟登录，登录页直接就是扫码界面）"""

    LOGIN_URL = "https://passport.bilibili.com/login"
    PLATFORM_ID = "bilibili"
    PLATFORM_NAME = "B站"

    # 需要保留的关键字段。
    # 注意：buvid3 是设备指纹，B站风控缺它会直接 412（Precondition Failed），
    # 所以绝不能只留 SESSDATA。
    KEY_FIELDS = ["SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "b_nut"]

    def _is_logged_in(self) -> bool:
        return bool(self._get_cookie_value("SESSDATA"))

    def _build_cookie(self) -> str:
        # 登录页在 passport.bilibili.com，直接 get_cookies() 只能拿到该域名下
        # 的 Cookie，会漏掉 .bilibili.com 域下的 buvid3 等设备指纹
        # （缺了 buvid3 → B站返回 412）。先跳到主站再读，确保拿到全量。
        try:
            self._driver.get("https://www.bilibili.com/")
            time.sleep(1.0)
        except Exception:
            pass

        try:
            cookie_dict = {
                c["name"]: c["value"]
                for c in self._driver.get_cookies()
                if c.get("name")
            }
        except Exception:
            return ""

        parts = [f"{k}={cookie_dict[k]}" for k in self.KEY_FIELDS if k in cookie_dict]
        return "; ".join(parts)


class DouyinCookieManager(_SeleniumLoginBase):
    """抖音 Cookie 获取管理器（Selenium 模拟登录）"""

    LOGIN_URL = "https://www.douyin.com"
    PLATFORM_ID = "douyin"
    PLATFORM_NAME = "抖音"

    # 需要保留的关键字段（其余无关字段一律丢弃）
    KEY_FIELDS = ["sessionid", "ttwid", "sid_guard", "odin_tt"]

    def _is_logged_in(self) -> bool:
        """
        判据只看 sessionid。
        注意：ttwid 是设备指纹，**未登录时就已经存在**，旧实现要求
        sessionid 与 ttwid 同时满足，等于多绕了一圈、白白拉长等待。
        """
        return bool(self._get_cookie_value("sessionid"))

    def _build_cookie(self) -> str:
        try:
            cookie_dict = {
                c["name"]: c["value"]
                for c in self._driver.get_cookies()
                if c.get("name")
            }
        except Exception:
            return ""
        parts = [f"{k}={cookie_dict[k]}" for k in self.KEY_FIELDS if k in cookie_dict]
        return "; ".join(parts)

    def _after_open(self, driver) -> None:
        """尽力自动点击「登录」并切到「扫码登录」，省去用户自己找入口。"""
        try:
            clicked = driver.execute_script(_AUTO_LOGIN_SCRIPT)
            print(f"[Cookie 管理器] 自动点击登录入口: {clicked}")
            if clicked:
                time.sleep(1.2)
                switched = driver.execute_script(_AUTO_SCAN_TAB_SCRIPT)
                print(f"[Cookie 管理器] 自动切换到扫码登录: {switched}")
        except Exception as e:
            print(f"[Cookie 管理器] 自动打开登录入口失败（可手动点击登录）: {e}")
