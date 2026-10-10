"""
下载任务队列

【职责】管理下载任务的生命周期（排队、运行、完成、失败）。
线程安全，供 GUI 层的并发调度逻辑使用。
"""
import threading
import uuid
import time
from typing import Optional


class DownloadTask:
    """单个下载任务的数据模型"""

    def __init__(self, url: str, video_info: dict, quality_id: int = None,
                 save_dir: str = "", audio_only: bool = False,
                 want_danmaku: bool = False, want_subtitle: bool = False):
        self.task_id: str = str(uuid.uuid4())[:8]
        self.url: str = url
        self.video_info: dict = video_info
        self.quality_id: int | None = quality_id
        self.quality_name: str = "仅音频" if audio_only else self._resolve_quality_name()
        self.save_dir: str = save_dir
        # 输出选项
        self.audio_only: bool = audio_only
        self.want_danmaku: bool = want_danmaku
        self.want_subtitle: bool = want_subtitle
        self.status: str = "pending"  # pending / downloading / completed / failed
        self.progress: float = 0.0
        self.downloaded: int = 0
        self.total: int = 0
        self.file_path: str = ""
        self.error_msg: str = ""
        self.created_at: float = time.time()
        # 取消标记：置位后正在进行的下载会尽快中断
        self.cancel_event = threading.Event()
        # 进度显示用：下载速度（字节/秒）与预计剩余秒数
        self.speed: float = 0.0
        self.eta: int = -1

    def cancel(self) -> None:
        """请求取消该任务（线程安全，只置位标记）"""
        self.cancel_event.set()

    @property
    def title(self) -> str:
        return self.video_info.get("title", "未知视频")

    @property
    def platform(self) -> str:
        return self.video_info.get("platform", "other")

    def _resolve_quality_name(self) -> str:
        """从 streams 中查找 quality_id 对应的清晰度名称"""
        if self.quality_id is None:
            return "默认"
        for s in self.video_info.get("streams", []):
            if s.get("type") == "video" and s.get("quality_id") == self.quality_id:
                return s.get("quality_name", "未知")
        return "未知"


class TaskQueue:
    """
    线程安全的任务队列。
    维护所有任务的有序列表，提供增删查改操作。
    """

    def __init__(self):
        self._tasks: list[DownloadTask] = []
        self._lock = threading.Lock()

    def add(self, task: DownloadTask) -> None:
        """添加任务到队列末尾"""
        with self._lock:
            self._tasks.append(task)

    def get_next_pending(self) -> Optional[DownloadTask]:
        """获取下一个等待中的任务（不移除），将其状态改为 downloading"""
        with self._lock:
            for task in self._tasks:
                if task.status == "pending":
                    task.status = "downloading"
                    return task
        return None

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """按 task_id 查找任务"""
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id:
                    return task
        return None

    def remove(self, task_id: str) -> bool:
        """移除指定任务（仅 pending 状态可移除）"""
        with self._lock:
            for i, task in enumerate(self._tasks):
                if task.task_id == task_id and task.status == "pending":
                    self._tasks.pop(i)
                    return True
        return False

    def promote(self, task_id: str) -> bool:
        """把指定待下载任务移到队首（置顶，优先下载）"""
        with self._lock:
            for i, task in enumerate(self._tasks):
                if task.task_id == task_id and task.status == "pending":
                    self._tasks.insert(0, self._tasks.pop(i))
                    return True
        return False

    def reset_for_retry(self, task_id: str) -> bool:
        """把失败/已取消的任务重置为待下载，供「重试」使用"""
        with self._lock:
            for task in self._tasks:
                if task.task_id == task_id and task.status in ("failed", "cancelled"):
                    task.status = "pending"
                    task.progress = 0.0
                    task.downloaded = 0
                    task.total = 0
                    task.speed = 0.0
                    task.eta = -1
                    task.error_msg = ""
                    task.cancel_event = threading.Event()
                    return True
        return False

    def all_tasks(self) -> list[DownloadTask]:
        """返回所有任务的快照列表"""
        with self._lock:
            return list(self._tasks)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks if t.status == "pending")

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks if t.status == "downloading")
