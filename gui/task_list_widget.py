"""
任务队列列表 UI 组件

【职责】展示下载任务队列的状态，每行一个任务：标题、清晰度、状态、
进度（进度条 + 速度/剩余时间/大小）、操作按钮。

【本轮增强】
- 下载中可「取消」、失败/已取消可「重试」、等待中可「置顶」；
- 进度列显示 已下载/总大小、速度、预计剩余时间。
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
from PySide6.QtGui import QFont, QFontMetrics

from core.task_queue import TaskQueue, DownloadTask

# 进度小字：固定为设备无关像素，保证与列宽（同样以设备无关像素计）一致。
# 注意：全局样式表里的 QLabel{font-size:13px} 会覆盖 setFont()，所以
# 下面在设置 setFont 的同时，也会在控件样式表里写同样的 font-size，两边保持一致。
_SMALL_FONT_PX = 10

# 用于推算「进度」列需要多宽的样本（最坏情况）
_PROGRESS_SAMPLE = "100% · 999.9M/999.9M · 99.9M/s · 59:59"


# 状态文本与颜色映射
_STATUS_MAP = {
    "pending": ("等待中", "#8A9099"),
    "downloading": ("下载中", "#FB7299"),
    "completed": ("已完成", "#1F9254"),
    "failed": ("失败", "#D9363E"),
    "cancelled": ("已取消", "#8A9099"),
}

# 复用的按钮样式
_BTN_BASE = ("QPushButton {{ background-color: {bg}; color: {fg}; "
             "border: 1px solid {bd}; border-radius: 6px; font-size: 12px; "
             "font-weight: 500; padding: 2px 9px; }} QPushButton:hover {{ background-color: {hv}; }}")
_BTN_GRAY = _BTN_BASE.format(bg="#FFFFFF", fg="#646A73", bd="#E1E4EA", hv="#F5F6F8")
_BTN_PINK = _BTN_BASE.format(bg="#FDEFF4", fg="#D44A74", bd="#FBCFE0", hv="#FBDFE9")
_BTN_GREEN = _BTN_BASE.format(bg="#EAF7F0", fg="#1F9254", bd="#BFE6D2", hv="#D8F0E4")
_BTN_RED = _BTN_BASE.format(bg="#FDECEC", fg="#D9363E", bd="#F7C6C7", hv="#FBD9D9")


def _fmt_size(num_bytes: float) -> str:
    """字节数格式化：B / KB / MB / GB"""
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / 1024 ** 2:.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{int(num_bytes)} B"


def _fmt_size_short(num_bytes: float) -> str:
    """紧凑体积格式（45.2M / 1.2G），用于窄列里显示"""
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.1f}G"
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / 1024 ** 2:.1f}M"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f}K"
    return f"{int(num_bytes)}B"


def _fmt_eta(seconds: int) -> str:
    """剩余秒数 → mm:ss / hh:mm:ss"""
    if seconds < 0:
        return "--"
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TaskListWidget(QWidget):
    """任务队列列表组件"""

    task_removed = Signal(str)    # 用户取消/移除任务
    task_retry = Signal(str)      # 用户点重试
    task_promote = Signal(str)    # 用户点置顶
    open_folder = Signal(str)     # 打开文件所在目录

    def __init__(self, task_queue: TaskQueue, parent=None):
        super().__init__(parent)
        self._task_queue = task_queue
        self._progress_bars: dict[str, QProgressBar] = {}
        self._progress_labels: dict[str, QLabel] = {}
        # 显式字体对象：用于列宽推算，同时保证渲染字号与推算字号一致
        self._small_font = QFont(self.font())
        self._small_font.setPixelSize(_SMALL_FONT_PX)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        header = QLabel("下载队列")
        header.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #1F2329; background: transparent;")
        layout.addWidget(header)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["标题", "清晰度", "状态", "进度", "操作"])
        header_view = self._table.horizontalHeader()

        # 列宽策略：
        #   标题   —— 自适应拉伸（占剩余空间）
        #   清晰度/状态 —— 按内容自适应，避免文字被截
        #   进度/操作 —— 给足宽度的固定列（可手动拖拽微调）
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)

        # 【关键】「进度」列宽度按真实字体度量动态推算，保证进度文字永远放得下
        # （之前写死 250/300px，13px 字体下文字实测 455px，必然被截断）
        progress_w = QFontMetrics(self._small_font).horizontalAdvance(_PROGRESS_SAMPLE) + 28
        self._table.setColumnWidth(3, max(260, progress_w))
        self._table.setColumnWidth(4, 150)
        header_view.setMinimumSectionSize(56)

        # 【关键】行高必须给够：进度格是「进度条 + 文字」两行，操作按钮本身高 26px，
        # 用默认行高（约 30px）会把内容纵向压扁裁掉，导致进度文字和按钮显示不全。
        vertical_header = self._table.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setDefaultSectionSize(60)
        vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

        self._table.setMinimumHeight(220)
        self._table.setMinimumWidth(560)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        # 宽度不够时可横向滚动，保证任何内容都够得着
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self._table)

    # ---- 刷新 ----

    def refresh(self):
        """重建整个任务列表（状态变化时调用）"""
        tasks = self._task_queue.all_tasks()
        self._table.setRowCount(len(tasks))
        self._progress_bars.clear()
        self._progress_labels.clear()

        for row, task in enumerate(tasks):
            title_item = QTableWidgetItem(task.title)
            title_item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            title_item.setToolTip(task.title)
            self._table.setItem(row, 0, title_item)

            self._table.setItem(row, 1, QTableWidgetItem(task.quality_name))

            status_text, status_color = _STATUS_MAP.get(task.status, ("未知", "#8A9099"))
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(Qt.GlobalColor.darkGray)
            status_item.setData(Qt.ItemDataRole.ForegroundRole, None)
            status_item.setToolTip(status_text)
            self._table.setItem(row, 2, status_item)

            self._table.setCellWidget(row, 3, self._create_progress_cell(task))
            self._table.setCellWidget(row, 4, self._create_action_buttons(task))

    def _create_progress_cell(self, task: DownloadTask) -> QWidget:
        """进度列：细进度条 + 文字（大小/速度/剩余）"""
        container = QWidget()
        box = QVBoxLayout(container)
        box.setContentsMargins(6, 2, 6, 2)
        box.setSpacing(2)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setStyleSheet("""
            QProgressBar { border: none; border-radius: 4px; background-color: #F0F1F3; }
            QProgressBar::chunk { background-color: #FB7299; border-radius: 4px; }
        """)

        label = QLabel("")
        label.setFont(self._small_font)
        # 样式表里同步写 font-size，避免被全局 QLabel 字号覆盖（两者必须一致）
        label.setStyleSheet(
            f"font-size: {_SMALL_FONT_PX}px; color: #8A9099; background: transparent;")

        # 依据状态填初值
        if task.status == "completed":
            bar.setValue(100)
            size = _fmt_size(task.total) if task.total else ""
            label.setText(f"100%  已完成  {size}".strip())
        elif task.status == "downloading":
            bar.setValue(int(task.progress))
            label.setText("准备中…")
        elif task.status == "cancelled":
            bar.setValue(0)
            label.setText("已取消")
        elif task.status == "failed":
            bar.setValue(0)
            label.setText("下载失败")
        else:
            bar.setValue(0)
            label.setText("等待下载")

        box.addWidget(bar)
        box.addWidget(label)

        self._progress_bars[task.task_id] = bar
        self._progress_labels[task.task_id] = label
        return container

    def _create_action_buttons(self, task: DownloadTask) -> QWidget:
        """根据任务状态生成操作按钮"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        def _btn(text: str, style: str, slot, tooltip: str = "") -> QPushButton:
            b = QPushButton(text)
            b.setFixedHeight(26)
            b.setStyleSheet(style)
            if tooltip:
                b.setToolTip(tooltip)
            b.clicked.connect(slot)
            return b

        if task.status == "pending":
            layout.addWidget(_btn("置顶", _BTN_GRAY,
                                  lambda: self.task_promote.emit(task.task_id),
                                  "移到队首，优先下载"))
            layout.addWidget(_btn("取消", _BTN_GRAY,
                                  lambda: self._on_remove(task.task_id)))
        elif task.status == "downloading":
            layout.addWidget(_btn("取消", _BTN_PINK, lambda t=task: t.cancel(),
                                  "中断当前下载"))
        elif task.status == "completed":
            layout.addWidget(_btn("打开", _BTN_GREEN,
                                  lambda: self._on_open_folder(task.file_path)))
        elif task.status == "failed":
            layout.addWidget(_btn("重试", _BTN_GRAY,
                                  lambda: self.task_retry.emit(task.task_id)))
            layout.addWidget(_btn("详情", _BTN_RED, lambda: self._show_error(task),
                                  task.error_msg))
        elif task.status == "cancelled":
            layout.addWidget(_btn("重试", _BTN_GRAY,
                                  lambda: self.task_retry.emit(task.task_id)))

        layout.addStretch()
        return container

    # ---- 进度更新 ----

    def update_progress(self, task_id: str, downloaded: int, total: int,
                        speed: float = 0.0, eta: int = -1):
        """更新指定任务的进度条与文字（下载过程中高频调用）"""
        bar = self._progress_bars.get(task_id)
        label = self._progress_labels.get(task_id)
        if bar is None:
            return

        if total > 0:
            percent = min(int(downloaded * 100 / total), 100)
            bar.setValue(percent)
            if label is not None:
                # 紧凑写法：单空格分隔，保证在列宽内完整显示
                text = f"{percent}% · {_fmt_size_short(downloaded)}/{_fmt_size_short(total)}"
                if speed > 0:
                    text += f" · {_fmt_size_short(speed)}/s · {_fmt_eta(eta)}"
                label.setText(text)
                # 悬停显示完整信息
                label.setToolTip(
                    f"已下载 {_fmt_size(downloaded)} / {_fmt_size(total)}\n"
                    f"速度 {_fmt_size(speed)}/s\n预计剩余 {_fmt_eta(eta)}")
        else:
            if label is not None:
                text = _fmt_size_short(downloaded)
                if speed > 0:
                    text += f"  ·  {_fmt_size_short(speed)}/s"
                label.setText(text)
                label.setToolTip(f"已下载 {_fmt_size(downloaded)}")

    def mark_completed(self, task_id: str):
        self.refresh()

    def mark_failed(self, task_id: str):
        self.refresh()

    def mark_cancelled(self, task_id: str):
        self.refresh()

    # ---- 事件 ----

    def _show_error(self, task: DownloadTask):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "下载失败", task.error_msg or "未知错误")

    def _on_remove(self, task_id: str):
        self._task_queue.remove(task_id)
        self.task_removed.emit(task_id)
        self.refresh()

    def _on_open_folder(self, file_path: str):
        """打开文件所在目录并选中文件"""
        if not file_path or not os.path.exists(file_path):
            return
        folder = os.path.dirname(file_path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(file_path)])
        else:
            subprocess.Popen(["xdg-open", folder])
