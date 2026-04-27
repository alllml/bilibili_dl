"""
Twitter/X 视频解析插件

【设计模式】具体策略类 ——
继承 BaseExtractor，实现 Twitter/X 平台的视频解析逻辑。
使用 Guest Token + GraphQL API 获取推文视频信息。
"""
import re
import requests

from core.base_extractor import BaseExtractor, VideoInfo
from core.downloader import download_file


# Twitter API 地址
_GUEST_TOKEN_API = "https://api.twitter.com/1.1/guest/activate.json"
_GRAPHQL_API = "https://twitter.com/i/api/graphql/0hWvDhmW8YQ-S_ib3azIrw/TweetResultByRestId"

# 固定的 Bearer Token（从 Twitter 网页提取，长期有效）
_BEARER_TOKEN = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# 通用请求头
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Authorization": _BEARER_TOKEN,
}

# 从 URL 中提取 Tweet ID 的正则
_TWEET_ID_PATTERN = re.compile(r"/status/(\d+)")

# 清晰度排序权重（bitrate 越高越好）
_QUALITY_PRIORITY = ["1080", "720", "480", "360", "240"]


class TwitterExtractor(BaseExtractor):
    """
    Twitter/X 视频解析器。
    支持 twitter.com 和 x.com 链接。
    使用 Guest Token 访问公开推文，无需登录。
    """

    SUPPORTED_DOMAINS = ["twitter.com", "x.com", "mobile.twitter.com"]
    PLATFORM_ID = "twitter"
    PLATFORM_NAME = "Twitter"
    REQUIRES_COOKIE = True

    def _extract_tweet_id(self, url: str) -> str | None:
        """从 URL 中提取 Tweet ID"""
        match = _TWEET_ID_PATTERN.search(url)
        if match:
            return match.group(1)
        return None

    def _get_guest_token(self) -> str:
        """
        获取 Guest Token（临时访问令牌）。
        Guest Token 用于无登录访问公开推文。
        """
        headers = dict(_DEFAULT_HEADERS)
        try:
            resp = requests.post(_GUEST_TOKEN_API, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            guest_token = data.get("guest_token")
            if not guest_token:
                raise RuntimeError("未能获取 Guest Token")
            print(f"  已获取 Guest Token: {guest_token[:20]}...")
            return guest_token
        except Exception as e:
            raise RuntimeError(f"获取 Guest Token 失败: {e}")

    def _fetch_tweet_data(self, tweet_id: str, guest_token: str, cookie: str = "") -> dict:
        """
        通过 GraphQL API 获取推文详情。
        返回包含视频信息的原始 JSON 数据。
        """
        headers = dict(_DEFAULT_HEADERS)
        headers["x-guest-token"] = guest_token

        # 如果提供了 Cookie，添加到请求头（用于访问私密内容）
        if cookie:
            headers["Cookie"] = cookie
            # 从 Cookie 中提取 ct0（CSRF Token）
            ct0_match = re.search(r"ct0=([^;]+)", cookie)
            if ct0_match:
                headers["x-csrf-token"] = ct0_match.group(1)

        # GraphQL 查询参数
        params = {
            "variables": '{"tweetId":"' + tweet_id + '","withCommunity":false,"includePromotedContent":false,"withVoice":false}',
            "features": '{"creator_subscriptions_tweet_preview_api_enabled":true,"communities_web_enable_tweet_community_results_fetch":true,"c9s_tweet_anatomy_moderator_badge_enabled":true,"articles_preview_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":true,"tweet_awards_web_tipping_enabled":false,"creator_subscriptions_quote_tweet_preview_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"rweb_tipjar_consumption_enabled":true,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_enhance_cards_enabled":false}',
        }

        try:
            resp = requests.get(_GRAPHQL_API, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"获取推文数据失败: {e}")

    def _parse_card_video(self, result: dict) -> dict | None:
        """
        从 Twitter 广告卡片中提取视频信息。
        返回视频媒体对象，如果没有视频则返回 None。
        """
        import json

        card = result.get("card", {})
        if not card:
            return None

        legacy = card.get("legacy", {})
        binding_values = legacy.get("binding_values", [])

        # 查找 unified_card 字段
        unified_card_str = None
        for binding in binding_values:
            if binding.get("key") == "unified_card":
                unified_card_str = binding.get("value", {}).get("string_value")
                break

        if not unified_card_str:
            return None

        try:
            # 解析 JSON 字符串
            unified_card = json.loads(unified_card_str)

            # 从 media_entities 中提取视频信息
            media_entities = unified_card.get("media_entities", {})

            # 遍历所有媒体实体，查找视频
            for media_id, media_data in media_entities.items():
                if media_data.get("type") == "video" and "video_info" in media_data:
                    return media_data

            return None
        except (json.JSONDecodeError, Exception):
            return None

    def _parse_video_variants(self, tweet_data: dict) -> list[dict]:
        """
        从推文数据中解析视频流列表。
        返回格式：[{"url": "...", "bitrate": 2176000, "quality_name": "720p"}, ...]
        """
        try:
            # 导航到 media 数据
            result = tweet_data.get("data", {}).get("tweetResult", {}).get("result", {})

            # 处理转发推文（Retweet）
            if result.get("__typename") == "TweetWithVisibilityResults":
                result = result.get("tweet", {})

            legacy = result.get("legacy", {})
            entities = legacy.get("extended_entities", {})
            media_list = entities.get("media", [])

            # 如果 extended_entities 中没有媒体，尝试从 card 中提取（广告卡片格式）
            if not media_list:
                video_media = self._parse_card_video(result)
                if not video_media:
                    raise RuntimeError("推文中未找到媒体内容")
            else:
                # 查找视频类型的媒体
                video_media = None
                for media in media_list:
                    media_type = media.get("type")
                    if media_type in ("video", "animated_gif"):
                        video_media = media
                        break

                if not video_media:
                    raise RuntimeError("推文中未找到视频内容（仅包含图片）")

            # 提取视频流列表
            video_info = video_media.get("video_info", {})
            variants = video_info.get("variants", [])

            # 过滤出 MP4 格式的流（排除 m3u8）
            mp4_variants = [
                v for v in variants
                if v.get("content_type") == "video/mp4"
            ]

            if not mp4_variants:
                raise RuntimeError("未找到可用的 MP4 视频流")

            # 解析清晰度信息
            parsed_variants = []
            for v in mp4_variants:
                url = v.get("url", "")
                bitrate = v.get("bitrate", 0)

                # 从 URL 中提取分辨率（如 1280x720）
                width, height = 0, 0
                resolution_match = re.search(r"/(\d+)x(\d+)/", url)
                if resolution_match:
                    width, height = int(resolution_match.group(1)), int(resolution_match.group(2))
                    quality_name = f"{height}p"
                else:
                    quality_name = f"{bitrate // 1000}kbps"

                parsed_variants.append({
                    "url": url,
                    "bitrate": bitrate,
                    "quality_name": quality_name,
                    "width": width,
                    "height": height,
                })

            # 按 bitrate 降序排序（最高清晰度在前）
            parsed_variants.sort(key=lambda x: x["bitrate"], reverse=True)
            return parsed_variants

        except KeyError as e:
            raise RuntimeError(f"解析推文数据结构失败: {e}")

    def extract_info(self, url: str, cookie: str = "") -> VideoInfo:
        """
        解析 Twitter/X 视频信息：
        1. 提取 Tweet ID
        2. 获取 Guest Token
        3. 请求 GraphQL API 获取推文数据
        4. 解析视频流列表
        """
        tweet_id = self._extract_tweet_id(url)
        if not tweet_id:
            raise ValueError(f"无法从 URL 中提取 Tweet ID: {url}")

        print(f"正在使用 TwitterExtractor 处理链接...")
        print(f"  Tweet ID: {tweet_id}")

        # 获取 Guest Token
        guest_token = self._get_guest_token()

        # 获取推文数据
        print("  正在获取推文数据...")
        tweet_data = self._fetch_tweet_data(tweet_id, guest_token, cookie)

        # 解析视频流
        variants = self._parse_video_variants(tweet_data)
        print(f"  找到 {len(variants)} 个视频流")

        # 提取推文基本信息
        result = tweet_data.get("data", {}).get("tweetResult", {}).get("result", {})
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})

        legacy = result.get("legacy", {})
        user_result = result.get("core", {}).get("user_results", {}).get("result", {})
        user_legacy = user_result.get("legacy", {})

        title = legacy.get("full_text", "Twitter视频")[:100]  # 截取前100字符作为标题
        owner = user_legacy.get("name", "未知用户")

        # 提取封面和时长信息
        cover_url = ""
        duration = 0

        # 从 video_media 中提取封面和时长
        if variants:
            # 尝试从第一个 variant 的父对象获取封面
            # 需要重新获取 video_media 对象
            video_media = None
            entities = legacy.get("extended_entities", {})
            media_list = entities.get("media", [])

            if not media_list:
                # 从 card 中获取
                video_media = self._parse_card_video(result)
            else:
                for media in media_list:
                    if media.get("type") in ("video", "animated_gif"):
                        video_media = media
                        break

            if video_media:
                # 提取封面
                cover_url = video_media.get("media_url_https", "")

                # 提取时长（毫秒转秒）
                video_info_data = video_media.get("video_info", {})
                duration_millis = video_info_data.get("duration_millis", 0)
                duration = duration_millis // 1000 if duration_millis else 0

        # 构造 streams 列表（供 GUI 选择清晰度）
        streams = [
            {
                "type": "video",
                "quality_id": i,
                "quality_name": v["quality_name"],
                "url": v["url"],
            }
            for i, v in enumerate(variants)
        ]

        # 默认选择最高清晰度
        best_variant = variants[0]

        return VideoInfo(
            title=title,
            cover_url=cover_url,
            video_url=best_variant["url"],
            audio_url="",  # Twitter 视频音视频已合并
            streams=streams,
            headers=dict(_DEFAULT_HEADERS),
            platform="twitter",
            duration=duration,
            owner=owner,
            view_count=0,
            danmaku_count=0,
        )

    def download(self, video_info: VideoInfo, save_path: str, capabilities: dict,
                 progress_callback=None) -> None:
        """
        Twitter 视频下载：
        Twitter 视频为音视频合并的 MP4 流，直接下载即可，无需 FFmpeg。
        下载 CDN 资源时去掉 Authorization 头，仅保留 User-Agent，避免被拦截。
        支持失败重试（最多 3 次）。
        """
        video_url = video_info.get("video_url", "")
        if not video_url:
            raise RuntimeError("未获取到视频播放地址，无法下载")

        # CDN 下载只需 User-Agent，不需要 Authorization
        download_headers = {
            "User-Agent": _DEFAULT_HEADERS["User-Agent"],
        }

        print("  正在下载 Twitter 视频...")

        import time

        last_error = None
        for attempt in range(1, 4):
            try:
                download_file(video_url, download_headers, save_path,
                              progress_callback=progress_callback)
                print(f"  下载完成: {save_path}")
                return
            except Exception as e:
                last_error = e
                print(f"  下载失败（第 {attempt} 次），错误: {type(e).__name__}: {e}")
                if attempt < 3:
                    time.sleep(2)
                    print(f"  正在重试...")

        raise RuntimeError(f"Twitter 视频下载失败（已重试 3 次）: {type(last_error).__name__}: {last_error}")
