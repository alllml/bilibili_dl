"""
抖音视频解析插件

【设计模式】具体策略类 ——
继承 BaseExtractor，实现抖音平台的视频解析与下载逻辑。
通过 Selenium 非 headless 模式渲染抖音视频页面，
从 RENDER_DATA 中提取视频信息，绕过抖音 API 签名验证。
登录 Cookie 下获取无水印视频下载地址。
"""
import json
import re
import time
from urllib.parse import unquote

import requests
from selenium import webdriver
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

from core.base_extractor import BaseExtractor, VideoInfo
from core.downloader import download_file


# 通用请求头（用于下载视频文件）
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

# 从 URL 中提取 aweme_id 的正则
_VIDEO_ID_PATTERN = re.compile(r"/video/(\d+)")
_MODAL_ID_PATTERN = re.compile(r"modal_id=(\d+)")
_NOTE_ID_PATTERN = re.compile(r"/note/(\d+)")
_SHARE_VIDEO_PATTERN = re.compile(r"/share/video/(\d+)")

# 清晰度 gear 名称映射
_GEAR_NAME_MAP = {
    "normal_1080_0": "1080P",
    "normal_720_0": "720P",
    "normal_540_0": "540P",
    "low_720_0": "720P(低码率)",
    "lower_540_0": "540P(低码率)",
    "adapt_lowest_0": "自适应",
}


class DouyinExtractor(BaseExtractor):
    """
    抖音视频解析器。
    支持 douyin.com 主站链接和 v.douyin.com 分享短链（短链由工厂层还原）。
    使用 Selenium headless 浏览器解析，无窗口弹出。

    【性能优化】浏览器常驻模式 ——
    首次调用时启动浏览器，后续复用同一实例，避免重复启动开销。
    批量下载多个抖音视频时速度提升明显（每个视频节省 2-5 秒启动时间）。
    """

    SUPPORTED_DOMAINS = ["douyin.com", "v.douyin.com", "m.douyin.com"]
    PLATFORM_ID = "douyin"
    PLATFORM_NAME = "抖音"
    REQUIRES_COOKIE = True

    # 类级别的浏览器实例缓存（所有 DouyinExtractor 实例共享）
    _shared_driver = None
    _driver_cookie = ""  # 记录当前浏览器的 Cookie 状态
    _parse_cache: dict[str, "VideoInfo"] = {}  # aweme_id → VideoInfo 缓存

    def _extract_aweme_id(self, url: str) -> str | None:
        """
        从 URL 中提取抖音视频 ID（aweme_id）。
        支持格式：
        - https://www.douyin.com/video/7xxxxxxxxxx
        - https://www.douyin.com/note/7xxxxxxxxxx
        - https://www.douyin.com/discover?modal_id=7xxxxxxxxxx
        """
        for pattern in (_VIDEO_ID_PATTERN, _NOTE_ID_PATTERN, _MODAL_ID_PATTERN, _SHARE_VIDEO_PATTERN):
            match = pattern.search(url)
            if match:
                return match.group(1)
        return None

    def _set_cookies(self, driver, cookie_str: str):
        """将 Cookie 字符串设置到浏览器"""
        for item in cookie_str.split("; "):
            if "=" in item:
                name, value = item.split("=", 1)
                try:
                    driver.add_cookie({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".douyin.com",
                    })
                except Exception:
                    pass

    @classmethod
    def _get_or_create_driver(cls, cookie: str = ""):
        """
        获取或创建浏览器实例（单例模式）。
        如果已有浏览器实例且 Cookie 未变化，直接复用；
        否则关闭旧实例，创建新实例并设置 Cookie。
        """
        # Cookie 变化时需要重启浏览器
        if cls._shared_driver is not None and cls._driver_cookie != cookie:
            print("  [抖音] Cookie 已变化，重启浏览器...")
            cls.close_browser()

        # 检查浏览器是否仍然可用
        if cls._shared_driver is not None:
            try:
                cls._shared_driver.execute_script("return 1")
            except Exception:
                print("  [抖音] 浏览器已失效，重新启动...")
                cls.close_browser()

        # 浏览器不存在或已关闭，创建新实例
        if cls._shared_driver is None:
            print("  [抖音] 启动常驻浏览器...")
            driver = cls._start_browser_static(headless=True)
            if driver is None:
                print("  headless 启动失败，尝试有头浏览器...")
                driver = cls._start_browser_static(headless=False)
            if driver is None:
                raise RuntimeError("无法启动浏览器（需要 Edge 或 Chrome）")

            cls._shared_driver = driver
            cls._driver_cookie = cookie

            # 设置 Cookie
            if cookie:
                try:
                    driver.get("https://www.douyin.com")
                    # 等待页面基本加载完成（DOM ready 即可设置 Cookie）
                    WebDriverWait(driver, 5).until(
                        lambda d: d.execute_script("return document.readyState") != "loading"
                    )
                    for item in cookie.split("; "):
                        if "=" in item:
                            name, value = item.split("=", 1)
                            try:
                                driver.add_cookie({
                                    "name": name.strip(),
                                    "value": value.strip(),
                                    "domain": ".douyin.com",
                                })
                            except Exception:
                                pass
                    print("  已设置 Cookie")
                except Exception as e:
                    print(f"  设置 Cookie 失败: {e}")

        return cls._shared_driver

    @staticmethod
    def _start_browser_static(headless: bool = True):
        """
        静态方法：启动浏览器（Edge → Chrome），反自动化检测。
        headless=True 时使用 --headless=new 模式（无窗口弹出）。
        headless=False 时启动有头浏览器并最小化窗口。
        返回 WebDriver 实例，失败返回 None。
        """
        mode_name = "headless" if headless else "有头"

        # 通用稳定性参数（防止 headless 模式下 tab crashed）
        _STABILITY_ARGS = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
        ]

        # Edge
        try:
            options = EdgeOptions()
            for arg in _STABILITY_ARGS:
                options.add_argument(arg)
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            if headless:
                options.add_argument("--headless=new")
            driver = webdriver.Edge(options=options)
            if not headless:
                driver.minimize_window()
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            })
            print(f"  [抖音] 使用 Edge 浏览器（{mode_name}）")
            return driver
        except WebDriverException as e:
            print(f"  [抖音] Edge {mode_name}启动失败: {e}")

        # Chrome
        try:
            options = ChromeOptions()
            for arg in _STABILITY_ARGS:
                options.add_argument(arg)
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            if headless:
                options.add_argument("--headless=new")
            driver = webdriver.Chrome(options=options)
            if not headless:
                driver.minimize_window()
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            })
            print(f"  [抖音] 使用 Chrome 浏览器（{mode_name}）")
            return driver
        except WebDriverException as e:
            print(f"  [抖音] Chrome {mode_name}启动失败: {e}")

        return None

    @classmethod
    def close_browser(cls):
        """
        关闭常驻浏览器实例。
        在程序退出或需要清理资源时调用。
        """
        if cls._shared_driver is not None:
            try:
                cls._shared_driver.quit()
                print("  [抖音] 已关闭常驻浏览器")
            except Exception as e:
                print(f"  [抖音] 关闭浏览器失败: {e}")
            finally:
                cls._shared_driver = None
                cls._driver_cookie = ""

    def extract_info(self, url: str, cookie: str = "") -> VideoInfo:
        """
        解析抖音视频信息（使用常驻浏览器）。
        首次调用时启动浏览器，后续复用同一实例。
        检测到 tab crashed 时自动重建浏览器重试一次。
        """
        aweme_id = self._extract_aweme_id(url)
        if not aweme_id:
            raise ValueError(f"无法从 URL 中提取抖音视频 ID: {url}")

        # 缓存命中：直接返回（同一视频重复解析时跳过浏览器渲染）
        cache_key = f"{aweme_id}:{cookie[:20] if cookie else ''}"
        if cache_key in DouyinExtractor._parse_cache:
            print(f"  [缓存命中] 视频 {aweme_id} 直接返回缓存结果")
            return DouyinExtractor._parse_cache[cache_key]

        print(f"正在使用 DouyinExtractor 处理链接...")
        print(f"  视频 ID: {aweme_id}")

        # 获取或创建常驻浏览器实例
        driver = self._get_or_create_driver(cookie)

        video_url = f"https://www.douyin.com/video/{aweme_id}"

        def _cache_and_return(result: VideoInfo) -> VideoInfo:
            DouyinExtractor._parse_cache[cache_key] = result
            return result

        try:
            driver.get(video_url)

            # 等待 RENDER_DATA 内容填充完成
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: len(d.find_element(By.ID, "RENDER_DATA").get_attribute("textContent") or "") > 100
                )
                # 额外等待 JS 填充完数据（部分视频 RENDER_DATA 元素先出现但内容延迟填充）
                time.sleep(2)
            except TimeoutException:
                print("  RENDER_DATA 等待超时，尝试继续解析...")

            detail = self._extract_detail_from_render_data(driver)

            if detail:
                return _cache_and_return(self._parse_from_render_data(detail, cookie))
            else:
                # RENDER_DATA 无数据时才走 DOM 降级路径，此时需要等待视频加载
                print("  RENDER_DATA 无视频详情，从页面 DOM 提取...")
                try:
                    WebDriverWait(driver, 5).until(
                        lambda d: d.execute_script(
                            'return document.querySelector("video") !== null')
                    )
                except TimeoutException:
                    pass
                return _cache_and_return(self._parse_from_dom(driver, cookie))

        except Exception as e:
            error_msg = str(e)
            # 检测到 tab crashed，销毁浏览器并重试一次
            if "tab crashed" in error_msg.lower() or "session deleted" in error_msg.lower():
                print(f"  检测到浏览器崩溃，正在重建浏览器并重试...")
                self.close_browser()
                # 重试一次
                try:
                    driver = self._get_or_create_driver(cookie)
                    driver.get(video_url)

                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.ID, "RENDER_DATA"))
                        )
                        time.sleep(2)
                    except TimeoutException:
                        pass

                    detail = self._extract_detail_from_render_data(driver)
                    if detail:
                        return _cache_and_return(self._parse_from_render_data(detail, cookie))
                    else:
                        print("  RENDER_DATA 无视频详情，从页面 DOM 提取...")
                        try:
                            WebDriverWait(driver, 5).until(
                                lambda d: d.execute_script(
                                    'return document.querySelector("video") !== null')
                            )
                        except TimeoutException:
                            pass
                        return _cache_and_return(self._parse_from_dom(driver, cookie))
                except Exception as retry_e:
                    print(f"  重试失败: {retry_e}")
                    raise
            # 其他错误直接抛出
            print(f"  解析出错: {e}")
            raise

    def _extract_detail_from_render_data(self, driver) -> dict:
        """
        从 RENDER_DATA 中提取视频详情。
        RENDER_DATA 的 JSON 结构不固定，顶层 key 可能是数字字符串（如 "0"、"C_12345"），
        需要递归搜索包含 aweme_id / video / desc 等关键字段的字典节点。
        """
        try:
            render_el = driver.find_element("id", "RENDER_DATA")
            raw = render_el.get_attribute("textContent")
            data = json.loads(unquote(raw))
        except Exception as e:
            print(f"  RENDER_DATA 解析失败: {e}")
            return {}

        # 调试：打印顶层结构
        print(f"  [调试] RENDER_DATA 顶层 keys: {list(data.keys())}")

        # 路径 1：标准结构 app.videoDetail
        app_data = data.get("app", {})
        detail = app_data.get("videoDetail", {})
        if detail and detail.get("video"):
            print("  从 RENDER_DATA.app.videoDetail 提取")
            return detail

        # 路径 2：直接搜索已知 key（awemeDetail / aweme_detail）
        _KNOWN_DETAIL_KEYS = ("awemeDetail", "aweme_detail", "videoDetail", "video_detail")
        for top_key, top_val in data.items():
            if not isinstance(top_val, dict):
                continue
            for detail_key in _KNOWN_DETAIL_KEYS:
                candidate = top_val.get(detail_key)
                if isinstance(candidate, dict) and candidate.get("video"):
                    print(f"  从 RENDER_DATA['{top_key}']['{detail_key}'] 直接提取")
                    return candidate

        # 路径 3：改进的递归深度搜索（优先匹配同时含 desc 和 video 的节点）
        _IDENTITY_KEYS = {"desc", "aweme_id", "awemeId"}

        def _has_video_subdict(obj: dict) -> bool:
            video = obj.get("video", {})
            return isinstance(video, dict) and (
                video.get("playAddr") or video.get("playApi") or
                video.get("play_addr") or video.get("bitRateList") or
                video.get("bit_rate") or video.get("playUrl") or
                video.get("play_url") or video.get("duration")
            )

        def find_video_detail(obj, depth=0):
            """递归搜索：优先返回同时含 desc 和 video 的节点"""
            if depth > 6 or not isinstance(obj, dict):
                return None

            has_video = _has_video_subdict(obj)
            has_identity = bool(_IDENTITY_KEYS & obj.keys())

            # 最佳匹配：同时有 video 和 desc/aweme_id
            if has_video and has_identity:
                return obj

            best_full = None
            best_video_only = None

            for key, val in obj.items():
                children = []
                if isinstance(val, dict):
                    children.append(val)
                elif isinstance(val, list):
                    children.extend(item for item in val if isinstance(item, dict))

                for child in children:
                    result = find_video_detail(child, depth + 1)
                    if result is None:
                        continue
                    if _IDENTITY_KEYS & result.keys():
                        if best_full is None:
                            best_full = result
                    elif best_video_only is None:
                        best_video_only = result
                    if best_full is not None:
                        return best_full

            if has_video:
                return obj

            return best_video_only

        detail = find_video_detail(data)

        # 向上回溯：如果找到的节点没有 desc，检查父节点
        if detail and not detail.get("desc") and not detail.get("title"):
            def _find_parent(root, target, depth=0):
                if depth > 6 or not isinstance(root, dict):
                    return None
                for key, val in root.items():
                    if val is target:
                        return root
                    if isinstance(val, dict):
                        parent = _find_parent(val, target, depth + 1)
                        if parent is not None:
                            return parent
                    elif isinstance(val, list):
                        for item in val:
                            if item is target:
                                return root
                            if isinstance(item, dict):
                                parent = _find_parent(item, target, depth + 1)
                                if parent is not None:
                                    return parent
                return None

            parent = _find_parent(data, detail)
            if parent and (parent.get("desc") or parent.get("title")):
                print("  向上回溯到含 desc 的父节点")
                detail = parent
        if detail:
            print("  从 RENDER_DATA 深度搜索提取到视频数据")
            print(f"  [调试] detail keys: {list(detail.keys())}")
            video = detail.get("video", {})
            print(f"  [调试] video keys: {list(video.keys()) if isinstance(video, dict) else type(video)}")
            return detail

        print("  RENDER_DATA 中未找到视频详情")
        # 调试：打印每个顶层 key 的子 key，帮助定位结构
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"  [调试] data['{k}'] keys: {list(v.keys())[:15]}")
        return {}

    def _parse_from_render_data(self, detail: dict, cookie: str) -> VideoInfo:
        """
        从 RENDER_DATA 的 videoDetail 解析视频信息。
        兼容 camelCase 和 snake_case 两种字段命名风格。
        """
        # 调试日志：打印关键字段结构
        print(f"  [调试] detail keys: {list(detail.keys())}")

        title = (detail.get("desc", "") or detail.get("title", "") or
                 detail.get("shareTitle", "") or detail.get("share_title", "") or
                 detail.get("caption", "") or "抖音视频")
        title = title.strip()

        # 作者信息（兼容多种字段路径）
        author_info = detail.get("authorInfo", {}) or detail.get("author", {})
        owner = ""
        if author_info:
            print(f"  [调试] author_info keys: {list(author_info.keys())}")
            owner = (author_info.get("nickname", "") or
                     author_info.get("nick_name", "") or
                     author_info.get("nickName", ""))
        if not owner:
            owner = detail.get("nickname", "") or detail.get("nickName", "")
        if not owner:
            owner = "未知"

        video = detail.get("video", {})
        print(f"  [调试] video keys: {list(video.keys())}")
        duration_ms = video.get("duration", 0)
        duration_sec = duration_ms // 1000 if duration_ms else 0

        # 封面提取（兼容字符串和对象两种结构）
        cover_url = ""
        # 路径1: video.cover 为字符串
        raw_cover = video.get("cover", "") or video.get("coverUrl", "")
        if isinstance(raw_cover, str) and raw_cover:
            cover_url = raw_cover
        # 路径2: video.cover 为对象 { url_list: [...] }
        if not cover_url:
            cover_obj = video.get("cover", {})
            if isinstance(cover_obj, dict):
                url_list = cover_obj.get("url_list", []) or cover_obj.get("urlList", [])
                if url_list:
                    cover_url = url_list[0]
        # 路径3: video.origin_cover
        if not cover_url:
            origin_cover = video.get("origin_cover", {}) or video.get("originCover", {})
            if isinstance(origin_cover, dict):
                url_list = origin_cover.get("url_list", []) or origin_cover.get("urlList", [])
                if url_list:
                    cover_url = url_list[0]
        # 路径4: video.dynamic_cover
        if not cover_url:
            dynamic_cover = video.get("dynamic_cover", {}) or video.get("dynamicCover", {})
            if isinstance(dynamic_cover, dict):
                url_list = dynamic_cover.get("url_list", []) or dynamic_cover.get("urlList", [])
                if url_list:
                    cover_url = url_list[0]

        print(f"  [调试] 提取到的封面: {cover_url[:80] if cover_url else '(空)'}")
        print(f"  [调试] 提取到的作者: {owner}")

        # 统计数据
        stats = detail.get("stats", {}) or detail.get("statistics", {})
        play_count = stats.get("playCount", 0) or stats.get("play_count", 0)
        comment_count = stats.get("commentCount", 0) or stats.get("comment_count", 0)

        # 视频播放地址（优先级：playAddr > playApi > play_addr）
        best_video_url = ""

        # 尝试 playAddr 列表
        play_addr_list = video.get("playAddr", []) or video.get("play_addr", [])
        if play_addr_list and isinstance(play_addr_list, list):
            for addr in play_addr_list:
                if isinstance(addr, dict):
                    url = addr.get("src", "") or addr.get("url", "")
                    if url:
                        best_video_url = url
                        break

        # 尝试 playApi
        if not best_video_url:
            play_api = video.get("playApi", "") or video.get("play_api", "")
            if play_api:
                best_video_url = play_api

        # 尝试 bitRateList 中的第一个可用地址
        if not best_video_url:
            bit_rate_list = video.get("bitRateList", []) or video.get("bit_rate_list", [])
            for br in bit_rate_list:
                addrs = br.get("playAddr", []) or br.get("play_addr", [])
                if addrs and isinstance(addrs, list):
                    for addr in addrs:
                        if isinstance(addr, dict):
                            url = addr.get("src", "") or addr.get("url", "")
                            if url:
                                best_video_url = url
                                break
                if best_video_url:
                    break

        if not best_video_url:
            raise RuntimeError("未获取到视频播放地址")

        # 清晰度列表
        streams = []
        seen_gears = set()
        bit_rate_list = video.get("bitRateList", []) or video.get("bit_rate_list", [])
        for br in bit_rate_list:
            gear = br.get("gearName", "") or br.get("gear_name", "")
            if gear in seen_gears:
                continue
            seen_gears.add(gear)
            addrs = br.get("playAddr", []) or br.get("play_addr", [])
            if not addrs:
                continue

            url = ""
            for addr in addrs:
                if isinstance(addr, dict):
                    url = addr.get("src", "") or addr.get("url", "")
                    if url:
                        break

            if not url:
                continue

            quality_name = _GEAR_NAME_MAP.get(gear, gear)
            streams.append({
                "quality_id": br.get("bitRate", 0) or br.get("bit_rate", 0),
                "quality_name": quality_name,
                "url": url,
                "codecs": "",
                "bandwidth": br.get("bitRate", 0) or br.get("bit_rate", 0),
                "type": "video",
            })

        print(f"  标题: {title}")
        print(f"  作者: {owner}")
        print(f"  时长: {duration_sec}秒")
        if streams:
            print(f"  可用清晰度: {len(streams)}")

        download_headers = dict(_DEFAULT_HEADERS)
        if cookie:
            download_headers["Cookie"] = cookie

        return VideoInfo(
            title=title, cover_url=cover_url,
            video_url=best_video_url, audio_url="",
            streams=streams, headers=download_headers,
            platform="douyin", duration=duration_sec,
            owner=owner, view_count=play_count,
            danmaku_count=comment_count,
        )

    # 占位视频/加载动画的 URL 特征，需要过滤掉
    _PLACEHOLDER_PATTERNS = [
        "uuu_265.mp4",
        "douyin-pc-web/uuu",
        "placeholder",
        "loading",
    ]

    def _is_placeholder_url(self, url: str) -> bool:
        """判断 URL 是否为占位视频（非真实内容）"""
        if not url:
            return True
        url_lower = url.lower()
        return any(p in url_lower for p in self._PLACEHOLDER_PATTERNS)

    def _parse_from_dom(self, driver, cookie: str) -> VideoInfo:
        """从页面 DOM 元素提取视频信息（降级路径）"""
        # 提取标题、作者、封面
        meta_info = driver.execute_script('''
            var r = {};
            // 标题：优先 h1，降级 og:title / document.title
            var h1 = document.querySelector("h1");
            r.title = h1 ? h1.textContent.trim() : "";
            if (!r.title) {
                var ogTitle = document.querySelector("meta[property='og:title']");
                r.title = ogTitle ? (ogTitle.getAttribute("content") || "") : "";
            }
            if (!r.title) {
                r.title = document.title || "";
                // 去掉抖音默认的页面标题后缀
                if (r.title.indexOf(" - 抖音") > -1) {
                    r.title = r.title.split(" - 抖音")[0].trim();
                }
            }
            // 作者：从用户主页链接中提取（抖音 class 名是混淆的，不可靠）
            r.author = "";
            var userLinks = document.querySelectorAll("a[href*='/user/']");
            for (var i = 0; i < userLinks.length; i++) {
                var text = userLinks[i].textContent.trim();
                // 跳过空文本、"我的"、纯数字等非作者名
                if (text && text !== "我的" && text.indexOf("喜欢") === -1 && text.length < 40) {
                    r.author = text;
                    break;
                }
            }
            // 降级：语义化选择器
            if (!r.author) {
                var authorEl = document.querySelector(
                    "[data-e2e='video-author-name'], " +
                    "span[class*='authorName'], " +
                    "[class*='nickname']"
                );
                r.author = authorEl ? authorEl.textContent.trim() : "";
            }
            // 降级：meta 标签
            if (!r.author) {
                var metaAuthor = document.querySelector("meta[name='author']");
                if (metaAuthor) r.author = metaAuthor.getAttribute("content") || "";
            }
            // 封面：从 meta og:image 或 video poster 提取
            var ogImage = document.querySelector("meta[property='og:image']");
            r.cover = ogImage ? (ogImage.getAttribute("content") || "") : "";
            if (!r.cover) {
                var videoEl = document.querySelector("video[poster]");
                r.cover = videoEl ? (videoEl.getAttribute("poster") || "") : "";
            }
            return r;
        ''')

        title = meta_info.get("title", "").strip() or "抖音视频"
        owner = meta_info.get("author", "") or "未知"
        cover_url = meta_info.get("cover", "")

        # 轮询等待真实视频 URL 加载（最多等待 8 秒，已在上层等待过 video 元素）
        best_video_url = ""
        duration_sec = 0
        for attempt in range(8):
            info = driver.execute_script('''
                var r = {};
                // 尝试所有 video 元素
                var videos = document.querySelectorAll("video");
                var urls = [];
                for (var i = 0; i < videos.length; i++) {
                    var v = videos[i];
                    if (v.src && v.src.indexOf("blob:") === -1) urls.push(v.src);
                    if (v.currentSrc && v.currentSrc.indexOf("blob:") === -1) urls.push(v.currentSrc);
                    // 检查 source 子元素
                    var sources = v.querySelectorAll("source");
                    for (var j = 0; j < sources.length; j++) {
                        if (sources[j].src) urls.push(sources[j].src);
                    }
                }
                r.video_urls = urls;
                // 视频时长
                var player = document.querySelector("video");
                r.duration = player ? (player.duration || 0) : 0;
                return r;
            ''')

            duration_sec = int(info.get("duration", 0) or 0)
            for url in info.get("video_urls", []):
                if url and not self._is_placeholder_url(url):
                    best_video_url = url
                    break

            if best_video_url:
                break

            if attempt < 7:
                time.sleep(0.5)

        # 如果 DOM 中找不到，尝试从网络日志中提取
        if not best_video_url:
            print("  DOM 中未找到真实视频地址，尝试从网络日志提取...")
            best_video_url = self._extract_video_url_from_network(driver)

        if not best_video_url:
            raise RuntimeError(
                "未获取到视频播放地址。游客模式下抖音可能限制了播放，"
                "请尝试配置抖音 Cookie 后重试")

        # 截断标题
        if len(title) > 80:
            title = title[:80]

        print(f"  标题: {title}")
        print(f"  作者: {owner}")
        print(f"  时长: {duration_sec}秒")
        print(f"  视频地址: {best_video_url[:80]}...")
        print("  (游客模式，视频可能带水印)")

        download_headers = dict(_DEFAULT_HEADERS)

        return VideoInfo(
            title=title, cover_url=cover_url,
            video_url=best_video_url, audio_url="",
            streams=[], headers=download_headers,
            platform="douyin", duration=duration_sec,
            owner=owner, view_count=0, danmaku_count=0,
        )

    def _extract_video_url_from_network(self, driver) -> str:
        """从浏览器性能日志中提取视频 URL（适用于 blob URL 场景）"""
        try:
            logs = driver.execute_script('''
                var entries = performance.getEntriesByType("resource");
                var videoUrls = [];
                for (var i = 0; i < entries.length; i++) {
                    var url = entries[i].name;
                    if (url.indexOf(".mp4") > -1 ||
                        url.indexOf("/play/") > -1 ||
                        url.indexOf("aweme/v1/play") > -1 ||
                        url.indexOf("video/tos") > -1) {
                        videoUrls.push(url);
                    }
                }
                return videoUrls;
            ''')

            if logs and len(logs) > 0:
                # 过滤掉占位视频
                real_urls = [u for u in logs if not self._is_placeholder_url(u)]
                if not real_urls:
                    return ""

                # 优先选择包含 play 的 URL（通常是真实播放地址）
                for url in real_urls:
                    if "/play/" in url or "aweme/v1/play" in url:
                        print(f"  从网络日志提取到视频地址: {url[:80]}...")
                        return url
                print(f"  从网络日志提取到视频地址: {real_urls[0][:80]}...")
                return real_urls[0]
        except Exception as e:
            print(f"  从网络日志提取视频地址失败: {e}")

        return ""

    def download(self, video_info: VideoInfo, save_path: str, capabilities: dict,
                 progress_callback=None) -> None:
        """
        抖音视频下载：
        抖音视频为音视频合并的完整流，直接下载即可。
        抖音 CDN 可能返回 302 重定向，需先解析最终地址再下载。
        """
        headers = video_info.get("headers", dict(_DEFAULT_HEADERS))
        video_url = video_info.get("video_url", "")

        if not video_url:
            raise RuntimeError("未获取到视频播放地址，无法下载")

        # 抖音的视频 URL 可能需要跟随重定向获取最终地址
        # 使用 HEAD 请求预解析，避免 stream 下载时重定向失败
        try:
            head_resp = requests.head(
                video_url, headers=headers, allow_redirects=True, timeout=15)
            final_url = head_resp.url
            if final_url != video_url:
                print(f"  重定向: {video_url[:60]}... -> {final_url[:60]}...")
                video_url = final_url
        except Exception as e:
            print(f"  预解析重定向失败（将使用原始 URL）: {e}")

        print("  正在下载抖音视频...")
        download_file(video_url, headers, save_path,
                      progress_callback=progress_callback)
        print(f"  下载完成: {save_path}")
