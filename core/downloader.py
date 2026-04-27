"""
公共下载工具函数

【职责】HTTP 分块下载 + 进度回报。
所有 Extractor 的 download() 方法共用此函数完成实际的文件下载，
避免每个插件重复实现 HTTP 下载逻辑。
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _make_session() -> requests.Session:
    """创建带自动重试的 Session，应对 CDN 连接不稳定。"""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_file(
    url: str,
    headers: dict,
    save_path: str,
    progress_callback=None,
) -> str:
    """
    HTTP 分块下载文件。

    参数：
        url: 下载地址
        headers: 请求头（必须包含 Referer 等，否则 B 站会 403）
        save_path: 文件保存路径
        progress_callback: 进度回调函数，签名 callback(downloaded_bytes, total_bytes)
                          total_bytes 可能为 0（服务器未返回 Content-Length）

    返回：保存路径
    """
    session = _make_session()
    resp = session.get(url, headers=headers, stream=True, timeout=(15, 300))
    resp.raise_for_status()

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    chunk_size = 65536  # 64KB

    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)

    return save_path
