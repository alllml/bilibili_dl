"""
单任务下载工作线程

【职责】在独立 QThread 中执行单个下载任务，通过 Qt 信号报告进度。
每个 TaskWorker 对应一个 DownloadTask，由 MainWindow 的并发调度逻辑管理。
"""
from PySide6.QtCore import QThread, Signal

from core.download_manager import DownloadManager
from core.task_queue import DownloadTask


class TaskWorker(QThread):
    """
    单任务下载工作线程。
    通过三个信号与 GUI 通信，每个信号携带 task_id 以区分多任务：
    - progress: 下载进度 (task_id, 已下载字节, 总字节)
    - finished_ok: 下载完成 (task_id, 文件路径)
    - error: 下载出错 (task_id, 错误信息)
    """
    progress = Signal(str, int, int)
    finished_ok = Signal(str, str)
    error = Signal(str, str)

    def __init__(self, task: DownloadTask, manager: DownloadManager,
                 parent=None):
        super().__init__(parent)
        self._task = task
        self._manager = manager

    @property
    def task_id(self) -> str:
        return self._task.task_id

    def run(self):
        task = self._task
        try:
            file_path = self._manager.start_download_with_info(
                video_info=task.video_info,
                save_dir=task.save_dir,
                quality_id=task.quality_id,
                progress_callback=lambda d, t: self.progress.emit(
                    task.task_id, d, t),
            )
            task.file_path = file_path
            task.status = "completed"
            self.finished_ok.emit(task.task_id, file_path)
        except Exception as e:
            task.status = "failed"
            task.error_msg = str(e)
            self.error.emit(task.task_id, str(e))
