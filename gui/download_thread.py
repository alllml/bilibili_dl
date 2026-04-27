"""
下载线程

【职责】在独立 QThread 中执行下载，通过 Qt 信号向 GUI 报告进度。
避免下载阻塞主线程导致界面卡死。
"""
from PySide6.QtCore import QThread, Signal

from core.download_manager import DownloadManager


class DownloadThread(QThread):
    """
    下载工作线程。
    通过三个信号与 GUI 通信：
    - progress: 下载进度 (已下载字节, 总字节)
    - finished_ok: 下载完成 (文件路径)
    - error: 下载出错 (错误信息)
    """
    progress = Signal(int, int)       # downloaded, total
    finished_ok = Signal(str)         # file_path
    error = Signal(str)               # error message

    def __init__(self, manager: DownloadManager, url: str, save_dir: str,
                 video_info: dict = None, quality_id: int = None,
                 parent=None):
        super().__init__(parent)
        self._manager = manager
        self._url = url
        self._save_dir = save_dir
        self._video_info = video_info
        self._quality_id = quality_id

    def run(self):
        try:
            if self._video_info is not None:
                # 使用已解析的 video_info + 指定清晰度下载
                file_path = self._manager.start_download_with_info(
                    self._video_info,
                    self._save_dir,
                    quality_id=self._quality_id,
                    progress_callback=self._on_progress,
                )
            else:
                file_path = self._manager.start_download(
                    self._url,
                    self._save_dir,
                    progress_callback=self._on_progress,
                )
            self.finished_ok.emit(file_path)
        except Exception as e:
            self.error.emit(str(e))

    def _on_progress(self, downloaded: int, total: int):
        """将进度转发为 Qt 信号"""
        self.progress.emit(downloaded, total)
