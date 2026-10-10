"""
单任务下载工作线程

【职责】在独立 QThread 中执行单个下载任务，通过 Qt 信号报告进度。
每个 TaskWorker 对应一个 DownloadTask，由 MainWindow 的并发调度逻辑管理。

【本轮增强】
- 支持取消：把 task.cancel_event 传给下载器，用户点「取消」后尽快中断；
- 进度附带下载速度与预计剩余时间，用于界面上更直观的展示。
"""
import time

from PySide6.QtCore import QThread, Signal

from core.download_manager import DownloadManager
from core.downloader import DownloadCancelled
from core.task_queue import DownloadTask


class TaskWorker(QThread):
    """
    单任务下载工作线程。
    信号（均携带 task_id 以区分多任务）：
    - progress:    (task_id, 已下载字节, 总字节, 速度(字节/秒), 剩余秒数)
    - finished_ok: (task_id, 文件路径)
    - error:       (task_id, 错误信息)
    - cancelled:   (task_id)
    """
    progress = Signal(str, int, int, float, int)
    finished_ok = Signal(str, str)
    error = Signal(str, str)
    cancelled = Signal(str)

    def __init__(self, task: DownloadTask, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self._task = task
        self._manager = manager

    @property
    def task_id(self) -> str:
        return self._task.task_id

    def run(self):
        task = self._task

        # 速度 / 剩余时间计算（指数平滑，避免数字乱跳）
        last_time = time.time()
        last_bytes = 0
        smooth_speed = 0.0

        def on_progress(downloaded: int, total: int):
            nonlocal last_time, last_bytes, smooth_speed
            now = time.time()
            dt = now - last_time
            if dt >= 0.5:
                instant = (downloaded - last_bytes) / dt
                smooth_speed = instant if smooth_speed <= 0 else smooth_speed * 0.7 + instant * 0.3
                last_time, last_bytes = now, downloaded
                task.speed = smooth_speed
                task.eta = int((total - downloaded) / smooth_speed) if (total and smooth_speed > 0) else -1
            task.downloaded = downloaded
            task.total = total
            self.progress.emit(task.task_id, downloaded, total, task.speed, task.eta)

        # 打上任务名，便于在日志里区分到底是哪个视频在下载/失败
        print(f"[任务] 开始下载: {task.title}（{task.quality_name}）")

        try:
            file_path = self._manager.start_download_with_info(
                video_info=task.video_info,
                save_dir=task.save_dir,
                quality_id=task.quality_id,
                progress_callback=on_progress,
                cancel_event=task.cancel_event,
                audio_only=task.audio_only,
                want_danmaku=task.want_danmaku,
                want_subtitle=task.want_subtitle,
            )
            task.file_path = file_path
            task.status = "completed"
            self.finished_ok.emit(task.task_id, file_path)
        except DownloadCancelled:
            task.status = "cancelled"
            task.progress = 0.0
            self.cancelled.emit(task.task_id)
        except Exception as e:
            task.status = "failed"
            task.error_msg = str(e)
            self.error.emit(task.task_id, str(e))
