"""
任务队列列表 UI 组件

【职责】展示下载任务队列的状态，每行一个任务，包含标题、清晰度、状态、进度条和操作按钮。
"""
import os
import subprocess
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QProgressBar, QPushButton,
    QHeaderView, QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal

from core.task_queue import TaskQueue, DownloadTask


# 状态文本与颜色映射
_STATUS_MAP = {
    "pending": ("等待中", "#999"),
    "downloading": ("下载中", "#fb7299"),
    "completed": ("已完成", "#52c41a"),
    "failed": ("失败", "#f5222d"),
}


class TaskListWidget(QWidget):
    """任务队列列表组件"""

    # 当用户点击删除任务时发出
    task_removed = Signal(str)  # task_id
    # 当用户点击打开目录时发出
    open_folder = Signal(str)   # file_path

    def __init__(self, task_queue: TaskQueue, parent=None):
        super().__init__(parent)
        self._task_queue = task_queue
        self._progress_bars: dict[str, QProgressBar] = {}
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        # 标题行
        header = QLabel("下载队列")
        header.setStyleSheet("font-size: 14px; font-weight: bold; color: #333;")
        layout.addWidget(header)

        # 任务表格
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["标题", "清晰度", "状态", "进度", "操作"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(1, 80)
        self._table.setColumnWidth(2, 70)
        self._table.setColumnWidth(3, 160)
        self._table.setColumnWidth(4, 80)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet("""
            QTableWidget { border: 1px solid #e8e8e8; border-radius: 6px;
                           background-color: #fff; gridline-color: #f0f0f0; font-size: 13px; }
            QTableWidget::item { padding: 6px 8px; }
            QTableWidget::item:selected { background-color: #fde8ef; color: #333; }
        """)
        layout.addWidget(self._table)

    def refresh(self):
        """刷新整个任务列表"""
        tasks = self._task_queue.all_tasks()
        self._table.setRowCount(len(tasks))
        self._progress_bars.clear()

        for row, task in enumerate(tasks):
            # 标题
            title_item = QTableWidgetItem(task.title)
            title_item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            title_item.setToolTip(task.title)
            self._table.setItem(row, 0, title_item)

            # 清晰度
            self._table.setItem(row, 1, QTableWidgetItem(task.quality_name))

            # 状态
            status_text, status_color = _STATUS_MAP.get(task.status, ("未知", "#999"))
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(Qt.GlobalColor.darkGray)
            status_item.setData(Qt.ItemDataRole.ForegroundRole, None)
            self._table.setItem(row, 2, status_item)

            # 进度条
            progress_bar = QProgressBar()
            progress_bar.setRange(0, 100)
            progress_bar.setValue(int(task.progress * 100) if task.status == "completed" else int(task.progress))
            if task.status == "completed":
                progress_bar.setValue(100)
            progress_bar.setStyleSheet("""
                QProgressBar { border: none; border-radius: 4px; background-color: #e0e0e0;
                               text-align: center; font-size: 11px; max-height: 16px; }
                QProgressBar::chunk { background-color: #fb7299; border-radius: 4px; }
            """)
            self._table.setCellWidget(row, 3, progress_bar)
            self._progress_bars[task.task_id] = progress_bar

            # 操作按钮
            btn = self._create_action_button(task)
            self._table.setCellWidget(row, 4, btn)

    def _create_action_button(self, task: DownloadTask) -> QWidget:
        """根据任务状态创建操作按钮"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        if task.status == "pending":
            btn = QPushButton("取消")
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton { background-color: #f5f5f5; color: #595959; "
                "border: 1px solid #d9d9d9; border-radius: 4px; font-size: 12px; "
                "font-weight: bold; padding: 2px 10px; }"
                "QPushButton:hover { background-color: #fff1f0; color: #cf1322; "
                "border-color: #ffa39e; }")
            btn.clicked.connect(lambda: self._on_remove(task.task_id))
            layout.addWidget(btn)
        elif task.status == "completed":
            btn = QPushButton("打开")
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton { background-color: #f6ffed; color: #389e0d; "
                "border: 1px solid #b7eb8f; border-radius: 4px; font-size: 12px; "
                "font-weight: bold; padding: 2px 10px; }"
                "QPushButton:hover { background-color: #d9f7be; }")
            btn.clicked.connect(lambda: self._on_open_folder(task.file_path))
            layout.addWidget(btn)
        elif task.status == "failed":
            btn = QPushButton("详情")
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton { background-color: #fff1f0; color: #cf1322; "
                "border: 1px solid #ffa39e; border-radius: 4px; font-size: 12px; "
                "font-weight: bold; padding: 2px 10px; }")
            btn.setToolTip(task.error_msg)
            layout.addWidget(btn)
        else:
            # downloading 状态不显示按钮
            pass

        return container

    def update_progress(self, task_id: str, downloaded: int, total: int):
        """更新指定任务的进度条"""
        bar = self._progress_bars.get(task_id)
        if bar and total > 0:
            bar.setValue(min(int(downloaded * 100 / total), 100))

    def mark_completed(self, task_id: str):
        """标记任务完成，刷新对应行"""
        self.refresh()

    def mark_failed(self, task_id: str):
        """标记任务失败，刷新对应行"""
        self.refresh()

    def _on_remove(self, task_id: str):
        self._task_queue.remove(task_id)
        self.task_removed.emit(task_id)
        self.refresh()

    def _on_open_folder(self, file_path: str):
        """打开文件所在目录"""
        if not file_path or not os.path.exists(file_path):
            return
        folder = os.path.dirname(file_path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(file_path)])
        else:
            subprocess.Popen(["xdg-open", folder])
