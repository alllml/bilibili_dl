"""
抖音视频解析插件

【设计模式】具体策略类 ——
继承 BaseExtractor，实现抖音平台的视频解析与下载逻辑。
通过 Selenium 非 headless 模式渲染抖音视频页面，
从 RENDER_DATA 中提取视频信息，绕过抖音 API 签名验证。
登录 Cookie 下获取无水印视频下载地址。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
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
from core.downloader import download_file, DownloadCancelled
from core.media import ffmpeg_extract_audio


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

# RENDER_DATA 体积上限：超过则跳过解析，避免 json.loads 一次性构造超大对象吃爆内存
_MAX_RENDER_DATA_CHARS = 8 * 1024 * 1024

# 递归搜索的节点访问预算：防止超大结构把解析线程卡死
_MAX_WALK_NODES = 200_000

# 清晰度 gear 名称映射
_GEAR_NAME_MAP = {
    "normal_1080_0": "1080P",
    "normal_720_0": "720P",
    "normal_540_0": "540P",
    "low_720_0": "720P(低码率)",
    "lower_540_0": "540P(低码率)",
    "adapt_lowest_0": "自适应",
}


def _extract_balanced_json(text: str, start: int) -> str:
    """
    从 text[start] 处的 '{' 开始，取出花括号配平的 JSON 子串。
    处理字符串内的引号与转义，避免被值里的 '{' '}' 干扰。
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _first_url(node) -> str:
    """
    从抖音各式字段结构中取出第一个可用 URL。

    抖音同一字段在不同页面/版本下结构不固定，可能长这样：
      - "https://..."                                  （字符串）
      - ["https://...", ...]                            （字符串列表）
      - [{"src": "https://..."}, ...]                   （对象列表）
      - {"url_list": ["https://...", ...]}              （对象+url_list）
      - {"url_list": [{"src": "..."}]}                  （混合嵌套）
    本函数统一递归处理，取不到返回空串。
    """
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        # 注意：不使用 uri 字段，它是内部标识而非可播放地址
        for key in ("url_list", "urlList", "src", "url"):
            if key in node:
                found = _first_url(node[key])
                if found:
                    return found
        return ""
    if isinstance(node, (list, tuple)):
        for item in node:
            found = _first_url(item)
            if found:
                return found
    return ""


class DouyinExtractor(BaseExtractor):
    """
    抖音视频解析器。
    支持 douyin.com 主站链接和 v.douyin.com 分享短链（短链由工厂层还原）。
    使用 Selenium headless 浏览器解析，无窗口弹出。

    【性能优化】浏览器常驻模式 ——
    首次调用时启动浏览器，后续复用同一实例，避免重复启动开销。
    批量下载多个抖音视频时速度提升明显（每个视频节省 2-5 秒启动时间）。
    """

    SUPPORTED_DOMAINS = ["douyin.com", "v.douyin.com", "iesdouyin.com"]
    PLATFORM_ID = "douyin"
    PLATFORM_NAME = "抖音"
    REQUIRES_COOKIE = True

    # 类级别的浏览器实例缓存（所有 DouyinExtractor 实例共享）
    _shared_driver = None
    _driver_cookie = ""  # 记录当前浏览器的 Cookie 状态
    _parse_cache: dict[str, "VideoInfo"] = {}  # aweme_id → VideoInfo 缓存

    # 播放地址候选字段（按优先级排列，覆盖 camelCase / snake_case 两种命名）
    _PLAY_ADDR_KEYS = (
        "playAddr", "play_addr", "playUrl", "play_url",
        "playApi", "play_api", "downloadAddr", "download_addr",
    )
    # 多码率列表候选字段
    _BITRATE_KEYS = ("bitRateList", "bit_rate_list", "bitRate", "bit_rate")

    @classmethod
    def _extract_play_url(cls, video) -> str:
        """
        从 video 节点提取可下载的播放地址。
        先按 _PLAY_ADDR_KEYS 逐个尝试，再退到多码率列表逐项尝试。
        兼容 play_addr 为 dict（url_list 结构）或 list（src 结构）等多种形态。
        """
        if not isinstance(video, dict):
            return ""
        for key in cls._PLAY_ADDR_KEYS:
            url = _first_url(video.get(key))
            if url:
                return url
        for key in cls._BITRATE_KEYS:
            for br in video.get(key) or []:
                if isinstance(br, dict):
                    url = cls._extract_play_url(br)
                    if url:
                        return url
        return ""

    @staticmethod
    def _dewatermark(url: str) -> str:
        """把带水印的 playwm 地址替换为无水印的 play 地址（抖音经典做法）"""
        if url and "playwm" in url:
            return url.replace("playwm", "play")
        return url

    @staticmethod
    def _guess_size(node: dict) -> int:
        """
        尽力从候选条目中推断文件体积（字节）。
        抖音对同一清晰度会推送多个候选，其中「被剥离音频流」的版本体积明显更小，
        因此体积可作为「是否为含音频完整流」的判据。
        """
        if not isinstance(node, dict):
            return 0
        # 体积信息可能在 play_addr 内部
        for pa_key in ("playAddr", "play_addr"):
            pa = node.get(pa_key)
            if isinstance(pa, dict):
                for k in ("data_size", "dataSize", "size"):
                    v = pa.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        return int(v)
        # 也可能直接挂在条目上
        for k in ("data_size", "dataSize", "size"):
            v = node.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    @staticmethod
    def _is_dash_url(url: str) -> bool:
        """
        粗略识别 DASH 分片地址（音视频分离，直接下载会得到没有声音的视频）。
        只做高置信度判断，避免误伤正常 progressive 地址。
        """
        u = (url or "").lower()
        return ".m4s" in u or "/dash/" in u

    @staticmethod
    def _collect_candidates(video_info: dict) -> list[str]:
        """
        收集可下载的候选地址（去重保序）：
          1. video_url（当前选用地址，GUI 选清晰度后即为所选档位）
          2. primary_url（若被清晰度选择覆盖，这里保留原始的音视频合一地址）
          3. 其余各清晰度地址，依次兜底
        """
        candidates: list[str] = []

        def _add(raw: str) -> None:
            url = DouyinExtractor._dewatermark(raw or "")
            if url and url not in candidates:
                candidates.append(url)

        _add(video_info.get("video_url", ""))
        _add(video_info.get("primary_url", ""))
        for s in video_info.get("streams") or []:
            _add(s.get("url", ""))
        return candidates

    @staticmethod
    def _probe_streams(path: str, ffmpeg_path: str):
        """
        用 ffmpeg 探测文件包含哪些流。
        返回 (是否有视频流, 是否有音频流)；无 ffmpeg 或探测失败返回 (None, None)。

        双重校验用途：
          1. 没有视频流 → 下到的根本不是视频（例如误抓到静态资源），必须换候选；
          2. 有视频没音频 → 抖音只给了纯视频流，也换候选再试。
        """
        if not ffmpeg_path:
            return None, None
        try:
            creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", path],
                capture_output=True, timeout=30,
                creationflags=creationflags, encoding="utf-8", errors="replace")
            output = (result.stderr or "") + (result.stdout or "")
            return ("Video:" in output), ("Audio:" in output)
        except Exception as e:
            print(f"  流探测失败（跳过检测）: {e}")
            return None, None

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
                # 先导航到 douyin.com（add_cookie 要求当前处于该域名下）。
                # 注意：加载超时不应导致跳过设置 Cookie，因此单独捕获。
                try:
                    driver.get("https://www.douyin.com")
                except TimeoutException:
                    print("  首页加载超时，停止加载后继续设置 Cookie")
                    cls._stop_loading(driver)
                except Exception as e:
                    print(f"  打开抖音首页失败: {e}")

                try:
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

        # 通用稳定性参数
        _STABILITY_ARGS = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            # 【重要】不能禁止自动播放！抖音的播放地址只有在播放器真正开始拉流后
            # 才会出现在网络资源里（DOM 里通常是 blob:）。上一版加了
            # user-gesture-required 后完全抓不到地址，这里改回允许自动播放，
            # 内存改为「限制 V8 堆 + 解析后离开页面」来控制。
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            # 限制渲染进程 V8 堆大小，避免单个页面把内存吃光
            "--js-flags=--max-old-space-size=512",
            "--disable-background-networking",
        ]

        def _configure(options):
            for arg in _STABILITY_ARGS:
                options.add_argument(arg)
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            # 【速度关键】none：driver.get() 立刻返回，完全不等页面加载。
            # normal 会等 load（抖音无限流永不触发，实测卡满 120s）；
            # eager 等 DOMContentLoaded，实测仍要干等 20s 超时。
            # 我们本来就是自己轮询 RENDER_DATA，所以完全不需要等。
            options.page_load_strategy = "none"
            if headless:
                options.add_argument("--headless=new")

        def _finalize(driver):
            # 兜底超时：即使 eager 策略下仍有卡住的情况，也最多等 20 秒
            driver.set_page_load_timeout(20)
            driver.set_script_timeout(15)
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            })
            return driver

        # Edge
        try:
            options = EdgeOptions()
            _configure(options)
            driver = _finalize(webdriver.Edge(options=options))
            if not headless:
                driver.minimize_window()
            print(f"  [抖音] 使用 Edge 浏览器（{mode_name}）")
            return driver
        except WebDriverException as e:
            print(f"  [抖音] Edge {mode_name}启动失败: {e}")

        # Chrome
        try:
            options = ChromeOptions()
            _configure(options)
            driver = _finalize(webdriver.Chrome(options=options))
            if not headless:
                driver.minimize_window()
            print(f"  [抖音] 使用 Chrome 浏览器（{mode_name}）")
            return driver
        except WebDriverException as e:
            print(f"  [抖音] Chrome {mode_name}启动失败: {e}")

        return None

    def _safe_get(self, driver, url: str) -> None:
        """
        打开页面：即使加载超时也继续解析。
        抖音页面（尤其精选/推荐流）永远不会触发 load 事件，不能等它「加载完」；
        eager 策略下通常很快返回，这里只是兜底，避免再出现卡满 120 秒的情况。
        """
        try:
            driver.get(url)
        except TimeoutException:
            # 【重要】这里不能调 window.stop()！
            # 实测：抖音页面脚本加载较慢时会先触发加载超时，一旦 stop() 掉，
            # 页面脚本被掐断 → 播放器不初始化 → 标题为空、readyState=0、
            # 247 个资源里 0 个媒体请求，什么都抓不到。
            # 正确做法：让页面在后台继续加载，我们改为轮询等待播放器就绪。
            print("  页面加载超时（脚本仍在后台加载），改为等待播放器就绪")
        except Exception as e:
            print(f"  页面打开异常，继续尝试解析: {e}")

    @staticmethod
    def _stop_loading(driver):
        """
        停止页面继续加载。仅在明确需要时调用（如退出前）。
        注意：解析过程中【不要】调用它 —— 会掐断页面脚本导致播放器不初始化。
        """
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    # 明显的静态资源特征：抓到这些说明不是视频，必须排除
    _STATIC_EXT_RE = re.compile(
        r"\.(js|css|png|jpe?g|webp|svg|gif|json|woff2?|ico|html)(\?|#|$)", re.IGNORECASE)
    _STATIC_HOST_HINTS = ("douyinstatic.com", "douyin-pc-web")

    @classmethod
    def _is_static_asset(cls, url: str) -> bool:
        """
        判断 URL 是否是静态资源（JS/CSS/图片等）而非视频流。
        实测踩坑：douyinstatic.com 上形如 /media/xxx 的静态资源曾被误当成视频地址，
        结果下载出一个损坏的「假 mp4」。
        """
        if not url:
            return True
        low = url.lower()
        if any(h in low for h in cls._STATIC_HOST_HINTS):
            return True
        return bool(cls._STATIC_EXT_RE.search(low))

    @staticmethod
    def _pause_media(driver):
        """暂停所有视频（保留 src，DOM 降级路径还要读它）"""
        try:
            driver.execute_script(
                "document.querySelectorAll('video').forEach(function(v){"
                "try{v.muted=true;v.pause();}catch(e){}});")
        except Exception:
            pass

    @staticmethod
    def _force_play(driver):
        """
        主动触发播放。
        抖音的播放地址只有在播放器真正开始拉流后才会出现在 performance 资源里，
        所以解析前先补一次 play()（失败静默忽略）。
        """
        try:
            driver.execute_script(
                "document.querySelectorAll('video').forEach(function(v){"
                "try{v.muted=true;var p=v.play();"
                "if(p&&p.catch){p.catch(function(){});}}catch(e){}});")
        except Exception:
            pass

    @staticmethod
    def _log_page_diagnostics(driver):
        """打印当前页面诊断信息，便于定位「为什么抓不到地址」"""
        try:
            info = driver.execute_script("""
                var vids = document.querySelectorAll('video');
                var res = performance.getEntriesByType('resource');
                var media = [];
                for (var i = 0; i < res.length; i++) {
                    var n = res[i].name;
                    if (n.indexOf('douyinvod') > -1 || n.indexOf('video/tos') > -1 ||
                        n.indexOf('.m4s') > -1 || n.indexOf('/play/') > -1 ||
                        n.indexOf('mime_type=video') > -1 || n.indexOf('mime_type=audio') > -1) {
                        media.push(n.slice(0, 130));
                    }
                }
                return {
                    url: location.href,
                    title: document.title,
                    video_count: vids.length,
                    video_src: vids.length ? (vids[0].src || '') : '',
                    video_current: vids.length ? (vids[0].currentSrc || '') : '',
                    video_ready: vids.length ? (vids[0].readyState || 0) : -1,
                    resource_total: res.length,
                    media_hits: media
                };
            """)
            print(f"  [诊断] 当前页: {str(info.get('url'))[:100]}")
            print(f"  [诊断] 标题: {str(info.get('title'))[:50]}")
            print(f"  [诊断] <video> 数量: {info.get('video_count')}，"
                  f"readyState: {info.get('video_ready')}")
            src = info.get("video_src") or info.get("video_current") or ""
            print(f"  [诊断] video src: {src[:110] or '(空)'}")
            hits = info.get("media_hits") or []
            print(f"  [诊断] 资源条目 {info.get('resource_total')} 个，疑似媒体 {len(hits)} 个")
            for m in hits[:5]:
                print(f"    · {m}")
        except Exception as e:
            print(f"  [诊断] 采集失败: {e}")

    @staticmethod
    def _release_page(driver):
        """解析完成后暂停播放并离开当前页，释放视频缓冲占用的内存"""
        DouyinExtractor._pause_media(driver)
        try:
            driver.get("about:blank")
        except Exception:
            pass

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

        def _cache_and_return(result: VideoInfo) -> VideoInfo:
            DouyinExtractor._parse_cache[cache_key] = result
            return result

        # ---- 轻量路径（优先）：完全不启动浏览器，用 requests 拿页面 SSR 数据 ----
        # 命中时解析只需 1~2 秒，且标题/作者/封面/时长/播放地址都齐全。
        detail = self._fetch_detail_via_requests(aweme_id, cookie)
        if detail:
            try:
                return _cache_and_return(self._parse_from_render_data(detail, cookie))
            except Exception as e:
                print(f"  轻量数据不完整（{e}），改用浏览器渲染")

        # ---- 浏览器渲染路径 ----
        driver = self._get_or_create_driver(cookie)

        # 候选页面地址（按优先级）：
        #   1. 精选页弹窗 /jingxuan?modal_id= —— 实测只有该页面的 RENDER_DATA
        #      带完整 videoDetail（标题/作者/时长/封面/各清晰度）
        #   2. 标准详情页 /video/{id} —— 作为兜底
        #   3. 用户原始链接（若与上面都不同）
        page_urls = [f"https://www.douyin.com/jingxuan?modal_id={aweme_id}",
                     f"https://www.douyin.com/video/{aweme_id}"]
        if url and url not in page_urls:
            page_urls.append(url)

        def _attempt(active_driver) -> VideoInfo:
            """依次尝试各个候选页面，任一成功即返回"""
            last_error = None
            for page_url in page_urls:
                print(f"  打开页面: {page_url}")
                self._safe_get(active_driver, page_url)

                # 等待 RENDER_DATA 出现（不必等页面完全加载）
                try:
                    WebDriverWait(active_driver, 10).until(
                        lambda d: len(d.find_element(
                            By.ID, "RENDER_DATA").get_attribute("textContent") or "") > 100)
                    time.sleep(1.0)
                except TimeoutException:
                    print("  RENDER_DATA 等待超时，尝试继续解析...")
                except Exception as e:
                    # 元素丢失/脚本报错等都不应中断整个解析，继续走后续来源
                    print(f"  等待 RENDER_DATA 出现异常，继续解析: {type(e).__name__}: {e}")

                try:
                    return _cache_and_return(
                        self._parse_current_page(active_driver, aweme_id, cookie))
                except Exception as e:
                    last_error = e
                    print(f"  该页面解析失败: {e}")

            raise last_error  # 所有候选页面均失败

        try:
            return _attempt(driver)
        except Exception as e:
            error_msg = str(e)
            # 检测到 tab crashed，销毁浏览器并重试一次
            if "tab crashed" in error_msg.lower() or "session deleted" in error_msg.lower():
                print("  检测到浏览器崩溃，正在重建浏览器并重试...")
                self.close_browser()
                driver = self._get_or_create_driver(cookie)
                return _attempt(driver)
            # 其他错误直接抛出
            print(f"  解析出错: {e}")
            raise

    def _parse_current_page(self, driver, aweme_id: str, cookie: str) -> VideoInfo:
        """
        在当前已加载的页面上，按可靠性从高到低依次尝试各来源：
          1. RENDER_DATA（SSR 数据，最理想）
          2. window._ROUTER_DATA（SSR 数据，分享页/部分详情页）
          3. 页面 DOM（不跳转，作为同页兜底）
          4. 分享页 iesdouyin.com（最后一次跳转兜底）
        全部失败时抛出带原因的可读错误。
        """
        try:
            detail = self._extract_detail_from_render_data(driver)
            if not detail:
                detail = self._extract_detail_from_router_data(driver)
            if detail:
                return self._parse_from_render_data(detail, cookie)

            # 当前页无结构化详情：先在同页尝试 DOM。
            # 关键：先主动触发播放并等一会儿——抖音的真实流地址通常只有
            # 播放器真正开始拉流后才会出现（DOM 里多为 blob:）。
            print("  当前页面无结构化详情，触发播放并等待流地址出现...")
            self._force_play(driver)
            time.sleep(2.5)
            self._log_page_diagnostics(driver)

            try:
                WebDriverWait(driver, 5).until(
                    lambda d: d.execute_script(
                        'return document.querySelector("video") !== null')
                )
            except TimeoutException:
                pass

            dom_error = None
            try:
                return self._parse_from_dom(driver, cookie)
            except Exception as e:
                dom_error = e

            # 最后兜底：跳转到分享页（服务端渲染，不依赖接口签名）
            print(f"  DOM 提取失败（{dom_error}），尝试分享页兜底...")
            detail = self._extract_from_share_page(driver, aweme_id)
            if detail:
                return self._parse_from_render_data(detail, cookie)

            raise RuntimeError(
                f"抖音解析失败：未能获取视频信息（{dom_error}）。"
                "可能原因：抖音 Cookie 未配置或已过期，或页面结构已变更。")
        finally:
            # 【内存保护】解析结束后离开当前页，释放视频缓冲占用的内存
            self._release_page(driver)

    def _extract_detail_from_render_data(self, driver) -> dict:
        """
        从 RENDER_DATA 中提取视频详情。
        RENDER_DATA 的 JSON 结构不固定，顶层 key 可能是数字字符串（如 "0"、"C_12345"），
        需要递归搜索包含 aweme_id / video / desc 等关键字段的字典节点。
        """
        try:
            render_el = driver.find_element("id", "RENDER_DATA")
            raw = render_el.get_attribute("textContent") or ""
            # 【内存保护】推荐/精选类页面的 RENDER_DATA 可能达数十 MB，
            # json.loads 会一次性构造全部对象，容易把内存吃满导致进程闪退。
            if len(raw) > _MAX_RENDER_DATA_CHARS:
                print(f"  RENDER_DATA 过大（{len(raw) / 1024 / 1024:.1f}MB），"
                      f"跳过解析以避免内存占用")
                return {}
            data = json.loads(unquote(raw))
        except Exception as e:
            print(f"  RENDER_DATA 解析失败: {e}")
            return {}

        return self._find_detail_in_state(data)

    def _find_detail_in_state(self, data: dict) -> dict:
        """
        在 SSR 状态字典（RENDER_DATA / _ROUTER_DATA 等）里寻找视频详情。
        结构不固定，顶层 key 可能是数字字符串，需要递归搜索。
        —— 抽成独立方法，浏览器路径与轻量 requests 路径共用同一套查找逻辑。
        """
        if not isinstance(data, dict):
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

        # 访问预算：超大结构下限制总遍历节点数，避免解析线程长时间卡住
        _budget = {"left": _MAX_WALK_NODES}

        def find_video_detail(obj, depth=0):
            """递归搜索：优先返回同时含 desc 和 video 的节点"""
            if depth > 6 or not isinstance(obj, dict) or _budget["left"] <= 0:
                return None
            _budget["left"] -= 1

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
            _parent_budget = {"left": _MAX_WALK_NODES}

            def _find_parent(root, target, depth=0):
                if depth > 6 or not isinstance(root, dict) or _parent_budget["left"] <= 0:
                    return None
                _parent_budget["left"] -= 1
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

    def _extract_detail_from_router_data(self, driver) -> dict:
        """
        从页面的 window._ROUTER_DATA（SSR 数据）中提取视频详情。

        抖音 PC 页的 RENDER_DATA 现在常常只含配置信息、不含视频详情；
        而「分享页 / 部分详情页」会把真实数据注入到 window._ROUTER_DATA，
        结构形如：loaderData['video_(id)/page'].videoInfoRes.item_list[0]。
        这里做一次通用扫描，取到 item_list 的第一项即标准 aweme 对象。
        """
        try:
            raw = driver.execute_script("return window._ROUTER_DATA || null")
        except Exception as e:
            print(f"  _ROUTER_DATA 读取失败: {e}")
            return {}

        if not isinstance(raw, dict):
            return {}

        loader = raw.get("loaderData")
        if not isinstance(loader, dict):
            return {}

        for key, node in loader.items():
            if not isinstance(node, dict):
                continue
            info_res = node.get("videoInfoRes") or node.get("videoInfo")
            if not isinstance(info_res, dict):
                continue
            item_list = info_res.get("item_list") or info_res.get("itemList") or []
            if item_list and isinstance(item_list[0], dict):
                print(f"  从 _ROUTER_DATA['{key}'] 提取视频详情")
                return item_list[0]

        print("  _ROUTER_DATA 中未找到视频详情")
        return {}

    def _fetch_detail_via_requests(self, aweme_id: str, cookie: str = "") -> dict:
        """
        【轻量路径】完全不启动浏览器，用 requests 直接请求页面并解析 SSR 数据。
        命中时整段解析只要 1~2 秒（浏览器路径要十几秒）。

        依次尝试：
          1. 精选页弹窗 https://www.douyin.com/jingxuan?modal_id={id}
             —— 实测该页面 HTML 里的 RENDER_DATA 带完整 videoDetail；
          2. 分享页 https://www.iesdouyin.com/share/video/{id}/
        """
        headers = dict(_DEFAULT_HEADERS)
        if cookie:
            headers["Cookie"] = cookie

        candidates = [
            f"https://www.douyin.com/jingxuan?modal_id={aweme_id}",
            f"https://www.iesdouyin.com/share/video/{aweme_id}/",
        ]
        for page_url in candidates:
            label = page_url[:52]
            try:
                resp = requests.get(page_url, headers=headers, timeout=12,
                                    allow_redirects=True)
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                print(f"  轻量请求失败（{label}）: {e}")
                continue

            detail = self._parse_ssr_detail_from_html(html)
            if detail:
                print(f"  轻量解析成功（{label}）")
                return detail
            print(f"  页面无可用 SSR 数据（{label}）")

        return {}

    def _parse_ssr_detail_from_html(self, html: str) -> dict:
        """从页面 HTML 中提取 RENDER_DATA / _ROUTER_DATA 并找出视频详情"""
        if not html:
            return {}

        # ---- 1) RENDER_DATA（<script id="RENDER_DATA">，内容是 URL 编码的 JSON）----
        idx = html.find('id="RENDER_DATA"')
        if idx == -1:
            idx = html.find("id='RENDER_DATA'")
        if idx != -1:
            gt = html.find(">", idx)
            end = html.find("</script>", gt)
            if gt != -1 and end != -1:
                blob = html[gt + 1:end].strip()
                if blob and len(blob) <= _MAX_RENDER_DATA_CHARS:
                    try:
                        data = json.loads(unquote(blob))
                        found = self._find_detail_in_state(data)
                        if found:
                            return found
                    except Exception as e:
                        print(f"  内嵌 RENDER_DATA 解析失败: {e}")

        # ---- 2) window._ROUTER_DATA（分享页常见）----
        marker = html.find("_ROUTER_DATA")
        if marker != -1:
            raw = _extract_balanced_json(html, html.find("{", marker))
            if raw:
                try:
                    loader = (json.loads(raw) or {}).get("loaderData") or {}
                    for key, node in loader.items():
                        if not isinstance(node, dict):
                            continue
                        info_res = node.get("videoInfoRes") or node.get("videoInfo")
                        if not isinstance(info_res, dict):
                            continue
                        items = info_res.get("item_list") or info_res.get("itemList") or []
                        if items and isinstance(items[0], dict):
                            print(f"  从 _ROUTER_DATA['{key}'] 提取到详情")
                            return items[0]
                except Exception as e:
                    print(f"  内嵌 _ROUTER_DATA 解析失败: {e}")

        return {}

    def _extract_from_share_page(self, driver, aweme_id: str) -> dict:
        """
        兜底策略：访问抖音分享页（SSR 渲染），从 _ROUTER_DATA 中取视频详情。
        分享页为服务端渲染，不依赖前端接口签名，稳定性好于 PC 详情页。
        """
        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        try:
            self._safe_get(driver, share_url)
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return !!window._ROUTER_DATA"))
        except TimeoutException:
            print("  分享页 _ROUTER_DATA 等待超时")
        except Exception as e:
            print(f"  分享页加载失败: {e}")
            return {}
        return self._extract_detail_from_router_data(driver)

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

        # 视频播放地址：统一走 _extract_play_url，兼容多种字段命名与嵌套结构
        best_video_url = self._dewatermark(self._extract_play_url(video))
        if not best_video_url:
            raise RuntimeError(
                "未获取到视频播放地址（抖音页面结构可能已变更，或当前为游客模式受限，"
                "请配置抖音 Cookie 后重试）")

        # 清晰度列表。
        # 【无声问题根因】抖音对同一清晰度(gear)会推送多个候选地址，其中混入了：
        #   ① 被剥离音频流的「纯视频」版本（体积明显更小）
        #   ② 长视频的 DASH 分片（音视频分离，直接下载必然无声）
        # 因此这里按 gear 去重时保留「体积最大」的候选，并过滤掉 DASH 流。
        best_by_gear: dict[str, dict] = {}
        for key in self._BITRATE_KEYS:
            for br in video.get(key) or []:
                if not isinstance(br, dict):
                    continue
                url = self._dewatermark(self._extract_play_url(br))
                if not url or self._is_dash_url(url):
                    continue
                gear = br.get("gearName", "") or br.get("gear_name", "") or "默认"
                size = self._guess_size(br)
                prev = best_by_gear.get(gear)
                if prev is not None and prev["_size"] >= size:
                    continue
                bandwidth = br.get("bitRate", 0) or br.get("bit_rate", 0)
                best_by_gear[gear] = {
                    "quality_id": bandwidth,
                    "quality_name": _GEAR_NAME_MAP.get(gear, gear),
                    "url": url,
                    "codecs": "",
                    "bandwidth": bandwidth,
                    "type": "video",
                    "_size": size,
                }

        streams = []
        for s in best_by_gear.values():
            s.pop("_size", None)
            streams.append(s)

        # 默认下载地址：优先用顶层 play_addr（通常为音视频合一的 progressive 流）
        if not best_video_url and streams:
            best_video_url = streams[0]["url"]

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

        # 轮询等待播放器开始拉流（最多约 12 秒）。
        # 抖音的真实地址往往要等播放器初始化后才出现，所以每轮都主动触发一次 play()，
        # 并定期检查网络资源——很多情况下 DOM 里只有 blob:，真地址只存在于网络请求里。
        best_video_url = ""
        duration_sec = 0
        for attempt in range(24):
            self._force_play(driver)
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
                if url and not self._is_placeholder_url(url) and not self._is_static_asset(url):
                    best_video_url = url
                    break

            if best_video_url:
                break

            # 每 2 秒顺带看一眼网络资源里的媒体地址
            if attempt % 4 == 3:
                best_video_url = self._extract_video_url_from_network(driver)
                if best_video_url:
                    break

            time.sleep(0.5)

        if not best_video_url:
            raise RuntimeError(
                "未获取到视频播放地址：播放器始终未开始拉流。"
                "请确认 Cookie 有效，或稍后重试")

        # 截断标题
        if len(title) > 80:
            title = title[:80]

        # 去水印 + 补全协议（DOM 中偶尔抓到以 // 开头的地址）
        best_video_url = self._dewatermark(best_video_url)
        if best_video_url.startswith("//"):
            best_video_url = "https:" + best_video_url

        print(f"  标题: {title}")
        print(f"  作者: {owner}")
        print(f"  时长: {duration_sec}秒")
        print(f"  视频地址: {best_video_url[:80]}...")
        if not cookie:
            print("  (游客模式，视频可能带水印)")

        # 关键修复：DOM 降级路径同样要带上 Cookie，否则抖音 CDN 返回 403 导致下载失败
        download_headers = dict(_DEFAULT_HEADERS)
        if cookie:
            download_headers["Cookie"] = cookie

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
                var hits = [];
                var staticRe = /\\.(js|css|png|jpe?g|webp|svg|gif|json|woff2?|ico|html)(\\?|#|$)/i;
                for (var i = 0; i < entries.length; i++) {
                    var url = entries[i].name;
                    var low = url.toLowerCase();
                    // 排除静态资源站与静态后缀（曾被误抓成「视频」导致下载损坏）
                    if (low.indexOf("douyinstatic.com") > -1 ||
                        low.indexOf("douyin-pc-web") > -1 ||
                        staticRe.test(low)) {
                        continue;
                    }
                    if (url.indexOf(".mp4") > -1 ||
                        url.indexOf(".m4s") > -1 ||
                        url.indexOf("/play/") > -1 ||
                        url.indexOf("aweme/v1/play") > -1 ||
                        url.indexOf("douyinvod.com") > -1 ||
                        url.indexOf("video/tos") > -1 ||
                        url.indexOf("mime_type=video") > -1 ||
                        url.indexOf("mime_type=audio") > -1) {
                        hits.push(url);
                    }
                }
                return hits;
            ''')

            if not logs:
                return ""

            # 过滤占位视频；并排除纯音频轨（我们要下的是视频）
            real_urls = [u for u in logs
                         if not self._is_placeholder_url(u)
                         and not self._is_static_asset(u)]
            if not real_urls:
                print("  网络资源中没有媒体地址（可能播放器未开始拉流）")
                return ""
            video_urls = [u for u in real_urls if "mime_type=audio" not in u]
            pool = video_urls or real_urls
            if not pool:
                return ""

            # MSE 的分片请求会带 range= 参数，直接下会得到残缺片段，因此优先选不带 range 的完整地址
            full_urls = [u for u in pool if "range=" not in u]
            if full_urls:
                pool = full_urls
            else:
                print("  [警告] 只找到带 range= 的分片地址，下载结果可能不完整")

            # 优先真正的媒体直链（douyinvod / video/tos），其次是 /play/ 形式
            for url in pool:
                if "video/tos" in url or "douyinvod" in url:
                    print(f"  从网络日志提取到视频地址: {url[:80]}...")
                    return url
            for url in pool:
                if "/play/" in url or "aweme/v1/play" in url:
                    print(f"  从网络日志提取到视频地址: {url[:80]}...")
                    return url

            print(f"  从网络日志提取到视频地址: {pool[0][:80]}...")
            return pool[0]
        except Exception as e:
            print(f"  从网络日志提取视频地址失败: {e}")

        return ""

    @staticmethod
    def _resolve_final_url(url: str, headers: dict) -> str:
        """
        跟随 302 预解析最终直链。
        HEAD 被 CDN 拒绝（部分节点对 HEAD 返回 403/405）时退回原始地址，
        后续 GET 依旧会自动跟随重定向，不影响下载。
        """
        try:
            resp = requests.head(url, headers=headers, allow_redirects=True, timeout=15)
            return resp.url or url
        except Exception as e:
            print(f"  预解析重定向失败（将使用原始 URL）: {e}")
            return url

    def download(self, video_info: VideoInfo, save_path: str, capabilities: dict,
                 progress_callback=None) -> None:
        """
        抖音视频下载。

        【无声问题修复】抖音同一清晰度会混入「无音轨的纯视频流」与 DASH 分片，
        解析阶段虽已尽量避开，但并非 100% 可靠，因此这里再加一道保险：
        下载完成后用 FFmpeg 检测音轨，若为纯视频流则自动切换到下一个候选地址重下。

        - 下载链路统一补齐 Referer / User-Agent / Cookie（缺 Cookie 是 403 主因）
        - 每个候选先 HEAD 预解析跟随 302，拿到最终直链后再下载
        - 全部失败时给出可操作的中文提示
        """
        candidates = self._collect_candidates(video_info)
        if not candidates:
            raise RuntimeError("未获取到视频播放地址，无法下载")

        # 合并默认头 + 解析阶段带下来的头，确保 Referer / UA / Cookie 齐全
        headers = dict(_DEFAULT_HEADERS)
        headers.update(video_info.get("headers") or {})

        capabilities = capabilities or {}
        ffmpeg_path = capabilities.get("ffmpeg_path", "")
        cancel_event = capabilities.get("cancel_event")
        connections = capabilities.get("connections", 1) or 1
        adaptive = bool(capabilities.get("adaptive_connections", True))

        # ---- 仅下载音频（MP3）：先下完整流（含音轨的候选），再抽取音频 ----
        if capabilities.get("audio_only"):
            if not ffmpeg_path:
                raise RuntimeError("「仅下载音频」需要 FFmpeg，请先在 设置 中指定 FFmpeg 路径")

            tmp_dir = tempfile.mkdtemp(prefix="douyin_audio_")
            tmp_video = os.path.join(tmp_dir, "video.mp4")
            try:
                self._download_candidates(candidates, headers, tmp_video,
                                          ffmpeg_path, cancel_event, progress_callback,
                                          connections, adaptive)
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("用户已取消下载")
                print("  正在提取音频并转换为 MP3...")
                ffmpeg_extract_audio(ffmpeg_path, tmp_video, save_path)
                if progress_callback:
                    progress_callback(100, 100)
                print(f"  音频已保存: {save_path}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        self._download_candidates(candidates, headers, save_path,
                                  ffmpeg_path, cancel_event, progress_callback,
                                  connections, adaptive)
        print(f"  下载完成: {save_path}")

    def _download_candidates(self, candidates: list, headers: dict, dest_path: str,
                             ffmpeg_path: str, cancel_event, progress_callback,
                             connections: int = 1, adaptive: bool = True) -> None:
        """
        依次尝试候选地址下载到 dest_path；含音轨校验，纯视频流会自动换下一个候选。
        全部失败时抛出可操作的中文错误。
        """
        last_error = None
        for idx, raw_url in enumerate(candidates, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("用户已取消下载")

            url = raw_url
            if url.startswith("//"):
                url = "https:" + url

            final_url = self._resolve_final_url(url, headers)
            if final_url != url:
                print(f"  重定向: {url[:60]}... -> {final_url[:60]}...")

            print(f"  正在下载抖音视频（候选 {idx}/{len(candidates)}）...")
            try:
                download_file(final_url, headers, dest_path,
                              progress_callback=progress_callback,
                              cancel_event=cancel_event,
                              connections=connections, adaptive=adaptive)
            except DownloadCancelled:
                raise
            except Exception as e:
                last_error = e
                print(f"  候选 {idx} 下载失败: {type(e).__name__}: {e}")
                continue

            # 流校验：先确认下到的确实是视频，再看有没有音轨
            has_video, has_audio = self._probe_streams(dest_path, ffmpeg_path)

            if has_video is False:
                # 下到的根本不是视频（典型：误抓到 JS/图片等静态资源）
                if idx < len(candidates):
                    print("  该候选不是有效视频（无视频流），自动切换下一个候选...")
                    continue
                raise RuntimeError(
                    "抖音下载失败：所有候选地址都不是有效视频文件。"
                    "通常是解析到的地址受页面结构变化影响，请更新 Cookie 后重试。")

            if has_audio is False:
                if idx < len(candidates):
                    print("  该候选为纯视频流（无音轨），自动切换下一个候选地址重下...")
                    continue
                print("  [警告] 所有候选地址均无音轨，该视频可能没有声音"
                      "（抖音对部分长视频只提供音视频分离的 DASH 流）")
            return

        raise RuntimeError(
            f"抖音视频下载失败：{type(last_error).__name__}: {last_error}\n"
            "可能原因：① 下载直链已过期（重新解析即可）；"
            "② Cookie 未配置或已失效（请在 设置 → 抖音 Cookie 更新）；"
            "③ 该视频有下载权限限制。")
