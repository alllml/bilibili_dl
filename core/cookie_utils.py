"""
Cookie 解析与规整工具

【为什么要这个】抖音等平台的 Cookie 分段非常多（几十个 name=value），
普通用户根本不知道要复制哪几个。本模块负责把用户「随便粘贴」的内容
（整段 Cookie、浏览器插件导出的 JSON、甚至只贴一个值）统一规整成
标准 Cookie 字符串，并自动挑出各平台真正需要的关键字段。

对外主要接口：
    analyze(text, platform_id)      -> (cookie_str, present, missing, recognized)
    build_cookie_str(text, platform) -> (cookie_str, present, missing)
"""
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import requests


# 各平台的关键字段规则：
#   required —— 缺失会导致功能不可用（会提示用户）
#   optional —— 有则保留，能提升稳定性/画质
PLATFORM_COOKIE_RULES: dict[str, dict[str, list[str]]] = {
    "bilibili": {
        "required": ["SESSDATA"],
        "optional": ["bili_jct", "DedeUserID", "buvid3", "buvid4", "b_nut"],
    },
    "douyin": {
        "required": ["sessionid"],
        "optional": ["ttwid", "sid_guard", "odin_tt"],
    },
    "twitter": {
        "required": ["auth_token"],
        "optional": ["ct0"],
    },
}

# 平台展示名（用于提示文案）
PLATFORM_DISPLAY = {
    "bilibili": "B站",
    "douyin": "抖音",
    "twitter": "Twitter",
}

# 剩余天数少于该值时提示「即将过期」
EXPIRING_THRESHOLD_DAYS = 3

# 在线校验用 UA
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _strip_header_prefix(text: str) -> str:
    """去掉用户可能连同复制进来的 'Cookie:' 前缀（含中文冒号）"""
    return re.sub(r"^\s*cookie\s*[:：]\s*", "", (text or "").strip(), flags=re.IGNORECASE)


def parse_pairs(text: str) -> list[tuple[str, str]]:
    """
    把各种粘贴格式解析成 [(name, value), ...]。
    兼容：
      - 标准形式：a=1; b=2
      - 换行分隔：a=1\\nb=2
      - 请求头：  Cookie: a=1; b=2
      - 插件导出：[{"name":"a","value":"1"}, ...] 或 {"a":"1", ...}
    """
    raw = _strip_header_prefix(text)
    if not raw:
        return []

    # ---- JSON 格式（EditThisCookie / Cookie-Editor 等插件导出）----
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if data is not None:
            pairs: list[tuple[str, str]] = []
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if "name" in item and "value" in item:
                    pairs.append((str(item["name"]).strip(), str(item["value"]).strip()))
                else:
                    for k, v in item.items():
                        pairs.append((str(k).strip(), str(v).strip()))
            if pairs:
                return pairs

    # ---- name=value 形式（分号或换行分隔）----
    pairs = []
    for part in re.split(r"[;\n\r]+", raw):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def analyze(text: str, platform_id: str) -> tuple[str, list[str], list[str], bool]:
    """
    解析并规整用户输入。

    返回 (cookie_str, present, missing, recognized)：
        cookie_str —— 规整后可直接保存的 Cookie 字符串
        present    —— 已识别到的关键字段名列表
        missing    —— 缺失的「必需」字段名列表
        recognized —— 是否成功识别出关键字段（False 表示按原样保留）
    """
    rules = PLATFORM_COOKIE_RULES.get(platform_id)
    raw = _strip_header_prefix(text)
    if not raw:
        required = list(rules["required"]) if rules else []
        return "", [], required, False

    # 未登记的平台：原样保存
    if rules is None:
        return raw, [], [], False

    pairs = parse_pairs(raw)

    # 用户只贴了一个「裸值」（例如只复制了 SESSDATA 的值）→ 自动补上字段名
    if not pairs:
        primary = rules["required"][0] if rules["required"] else None
        if primary:
            return f"{primary}={raw}", [primary], [], True
        return raw, [], [], False

    lookup = {name.lower(): (name, value) for name, value in pairs}

    kept: list[str] = []
    present: list[str] = []
    for name in [*rules["required"], *rules["optional"]]:
        hit = lookup.get(name.lower())
        if hit:
            present.append(hit[0])
            kept.append(f"{hit[0]}={hit[1]}")

    # 一个关键字段都没识别到：原样保留，避免丢信息
    if not kept:
        return raw, [], list(rules["required"]), False

    missing = [n for n in rules["required"] if n.lower() not in lookup]
    return "; ".join(kept), present, missing, True


def build_cookie_str(text: str, platform_id: str) -> tuple[str, list[str], list[str]]:
    """规整 Cookie 字符串，返回 (cookie_str, present, missing)。"""
    cookie, present, missing, _ = analyze(text, platform_id)
    return cookie, present, missing


# ==================== Cookie 有效期识别 ====================
# 好消息：B站与抖音的 Cookie 里本身就编码了过期时间，可以完全离线解析，
# 不需要联网就能判断「这个很久以前配置的 Cookie 到底还能不能用」。


def _parse_ts(value) -> int | None:
    """字符串 → 合理的 Unix 时间戳（超出 2000~2100 年视为无效）"""
    try:
        ts = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ts if 946684800 < ts < 4102444800 else None


def _parse_http_date(value: str) -> int | None:
    """'Sun, 14-Jun-2026 06:49:39 GMT' → Unix 时间戳"""
    try:
        dt = parsedate_to_datetime((value or "").strip())
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, IndexError):
        return None


def cookie_expire_at(platform_id: str, cookie: str) -> int | None:
    """
    从 Cookie 中离线解析过期时间（Unix 秒），解析不出返回 None。

    - B站   SESSDATA ：`<uid>%2C<过期时间戳>%2C<md5>`
    - 抖音  sid_guard：`<sid>%7C<登录时间>%7C<有效期秒数>%7C<过期日期>`
                       优先用第 4 段的日期，退化为「登录时间 + 有效期秒数」
    """
    if not cookie:
        return None

    pairs = {name.lower(): value for name, value in parse_pairs(cookie)}

    if platform_id == "bilibili":
        raw = pairs.get("sessdata")
        if not raw:
            return None
        parts = unquote(raw).split(",")
        return _parse_ts(parts[1]) if len(parts) >= 2 else None

    if platform_id == "douyin":
        raw = pairs.get("sid_guard")
        if not raw:
            return None
        parts = unquote(raw).split("|")
        if len(parts) >= 4:
            by_date = _parse_http_date(parts[3])
            if by_date:
                return by_date
        if len(parts) >= 3:
            login_ts = _parse_ts(parts[1])
            try:
                max_age = int(parts[2])
            except (TypeError, ValueError):
                max_age = 0
            if login_ts and max_age > 0:
                return login_ts + max_age
        return None

    return None


def format_expire_time(ts: int | None) -> str:
    """时间戳 → 'YYYY-mm-dd HH:MM'"""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def check_cookie_status(platform_id: str, cookie: str) -> dict:
    """
    离线判断 Cookie 可用状态，返回：
        {state, expire_at, days_left, hint}
    state 取值：missing / expired / expiring / valid / unknown
    """
    if not cookie or not cookie.strip():
        return {"state": "missing", "expire_at": None, "days_left": None, "hint": "未配置"}

    expire_at = cookie_expire_at(platform_id, cookie)
    if expire_at is None:
        return {"state": "unknown", "expire_at": None, "days_left": None,
                "hint": "已配置（无法判断有效期）"}

    seconds_left = expire_at - int(time.time())
    days_left = seconds_left / 86400

    if seconds_left <= 0:
        return {"state": "expired", "expire_at": expire_at, "days_left": int(days_left),
                "hint": f"已过期！（{format_expire_time(expire_at)} 到期）"}
    if days_left <= EXPIRING_THRESHOLD_DAYS:
        return {"state": "expiring", "expire_at": expire_at, "days_left": int(days_left),
                "hint": f"即将过期（剩余 {int(days_left)} 天）"}
    return {"state": "valid", "expire_at": expire_at, "days_left": int(days_left),
            "hint": f"有效（{int(days_left)} 天后过期）"}


def verify_cookie_online(platform_id: str, cookie: str):
    """
    在线校验 Cookie 是否真的还能用（目前仅 B站有公开的登录态校验接口）。
    返回 (结果, 说明)：True/False 为明确结论，None 表示无法校验（附原因）。
    """
    if not cookie or not cookie.strip():
        return False, "未配置 Cookie"

    if platform_id == "bilibili":
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={"User-Agent": _UA,
                         "Referer": "https://www.bilibili.com",
                         "Cookie": cookie},
                timeout=10)
            data = resp.json()
            if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
                uname = (data.get("data") or {}).get("uname") or ""
                return True, f"有效，已登录账号：{uname}"
            return False, "已失效（B站接口返回未登录）"
        except requests.exceptions.SSLError:
            return None, ("网络连接被中断（SSL 错误），无法完成校验。"
                          "这不代表 Cookie 失效，通常是网络 / 代理 / 防火墙问题。")
        except requests.exceptions.RequestException as e:
            return None, (f"网络请求失败（{type(e).__name__}），无法完成校验。"
                          "这不代表 Cookie 失效，请检查网络后重试。")
        except ValueError:
            # resp.json() 解析失败：通常是被网络中间层拦截后返回了非 JSON 内容
            return None, "接口返回内容异常（可能被网络中间层拦截），无法完成校验。"
        except Exception as e:
            return None, f"校验过程出错：{type(e).__name__}: {e}"

    return None, "该平台暂不支持在线校验，请参考上方有效期"
