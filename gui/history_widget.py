"""
下载历史记录 UI 组件

【职责】展示下载历史记录列表，支持搜索、平台筛选、文件存在性检查。
"""
import os
import subprocess
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QPushButton,
    QHeaderView, QAbstractItemView, QLineEdit, QComboBox,
    QMessageBox,
)
from PySide6.QtCore import Qt

from core.database import DownloadHistoryDB


def _format_size(size_bytes: int) -> str:
    """将字节数格式化为易读形式"""
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f} GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


class HistoryWidget(QWidget):
    """下载历史记录组件"""

    PAGE_SIZE = 50

    def __init__(self, db: DownloadHistoryDB, factory=None, parent=None):
        super().__init__(parent)
        self._db = db
        self._factory = factory  # 用于动态获取平台名称
        self._current_page = 0
        self._current_platform = None  # None 表示全部
        self._search_keyword = ""
        self._init_ui()

    def _get_platform_name(self, platform_id: str) -> str:
        """根据 platform_id 获取显示名称"""
        if not self._factory:
            # 回退到硬编码映射
            return {"bilibili": "B站", "douyin": "抖音", "twitter": "Twitter"}.get(platform_id, platform_id)

        platforms = self._factory.get_registered_platforms()
        for p in platforms:
            if p["platform_id"] == platform_id:
                return p["platform_name"]
        return platform_id

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        # 搜索 + 筛选栏
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索标题 / UP主...")
        self._search_input.returnPressed.connect(self._on_search)
        filter_row.addWidget(self._search_input, 1)

        search_btn = QPushButton("搜索")
        search_btn.setFixedWidth(64)
        search_btn.setStyleSheet("QPushButton { padding: 8px 12px; }")
        search_btn.clicked.connect(self._on_search)
        filter_row.addWidget(search_btn)

        platform_label = QLabel("平台：")
        platform_label.setStyleSheet("font-size: 13px; color: #646A73; background: transparent;")
        filter_row.addWidget(platform_label)

        self._platform_combo = QComboBox()
        self._platform_combo.addItem("全部", None)
        # 动态添加已注册的平台
        if self._factory:
            for p in self._factory.get_registered_platforms():
                self._platform_combo.addItem(p["platform_name"], p["platform_id"])
        else:
            self._platform_combo.addItem("B站", "bilibili")
            self._platform_combo.addItem("抖音", "douyin")
            self._platform_combo.addItem("Twitter", "twitter")
        self._platform_combo.setFixedWidth(80)
        self._platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        filter_row.addWidget(self._platform_combo)

        layout.addLayout(filter_row)

        # 记录表格
        # 列宽策略与「下载队列」保持一致：
        #   标题 —— 拉伸；其余文字列 —— 按内容自适应（避免文字被截）；
        #   操作 —— 给足宽度的可拖拽列。
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            ["标题", "平台", "清晰度", "大小", "日期", "状态", "操作"])
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4, 5):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(6, 150)
        header_view.setMinimumSectionSize(56)

        # 行高给够：操作按钮本身 26px + 上下边距，默认行高（约 30px）会把按钮压扁
        vertical_header = self._table.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setDefaultSectionSize(48)
        vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

        self._table.setMinimumHeight(220)
        self._table.setMinimumWidth(560)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self._table, 1)

        # 底部分页栏
        page_row = QHBoxLayout()
        self._total_label = QLabel("共 0 条记录")
        self._total_label.setStyleSheet("font-size: 12px; color: #8A9099; background: transparent;")
        page_row.addWidget(self._total_label)
        page_row.addStretch()

        self._prev_btn = QPushButton("上一页")
        self._prev_btn.setFixedWidth(70)
        self._prev_btn.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #646A73; border: 1px solid #E1E4EA; "
            "border-radius: 6px; font-size: 12px; font-weight: 500; padding: 4px; }"
            "QPushButton:hover { background-color: #F5F6F8; }"
            "QPushButton:disabled { color: #C2C7CE; background-color: #F7F8FA; }")
        self._prev_btn.clicked.connect(self._on_prev_page)
        page_row.addWidget(self._prev_btn)

        self._page_label = QLabel("1 / 1")
        self._page_label.setStyleSheet("font-size: 12px; color: #646A73; background: transparent;")
        page_row.addWidget(self._page_label)

        self._next_btn = QPushButton("下一页")
        self._next_btn.setFixedWidth(70)
        self._next_btn.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #646A73; border: 1px solid #E1E4EA; "
            "border-radius: 6px; font-size: 12px; font-weight: 500; padding: 4px; }"
            "QPushButton:hover { background-color: #F5F6F8; }"
            "QPushButton:disabled { color: #C2C7CE; background-color: #F7F8FA; }")
        self._next_btn.clicked.connect(self._on_next_page)
        page_row.addWidget(self._next_btn)

        layout.addLayout(page_row)

    # ---- 数据加载 ----

    def refresh(self):
        """刷新当前页数据"""
        if self._search_keyword:
            records = self._db.search(self._search_keyword, limit=self.PAGE_SIZE)
            total = len(records)
        else:
            total = self._db.get_total_count(self._current_platform)
            records = self._db.get_records(
                platform=self._current_platform,
                limit=self.PAGE_SIZE,
                offset=self._current_page * self.PAGE_SIZE)

        total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._total_label.setText(f"共 {total} 条记录")
        self._page_label.setText(f"{self._current_page + 1} / {total_pages}")
        self._prev_btn.setEnabled(self._current_page > 0)
        self._next_btn.setEnabled(self._current_page < total_pages - 1)

        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            # 标题
            title_item = QTableWidgetItem(rec.get("title", ""))
            title_item.setData(Qt.ItemDataRole.UserRole, rec.get("id"))
            title_item.setToolTip(rec.get("title", ""))
            self._table.setItem(row, 0, title_item)

            # 平台
            platform = rec.get("platform", "")
            self._table.setItem(
                row, 1, QTableWidgetItem(self._get_platform_name(platform)))

            # 清晰度
            self._table.setItem(
                row, 2, QTableWidgetItem(rec.get("quality_name", "")))

            # 大小
            self._table.setItem(
                row, 3, QTableWidgetItem(_format_size(rec.get("file_size", 0))))

            # 日期
            created = rec.get("created_at", "")
            # 只显示日期部分（去掉秒）
            if len(created) > 16:
                created = created[:16]
            self._table.setItem(row, 4, QTableWidgetItem(created))

            # 状态（检查文件是否存在）
            file_path = rec.get("file_path", "")
            status = rec.get("status", "")
            if status == "completed" and file_path:
                exists = os.path.exists(file_path)
                if exists:
                    status_item = QTableWidgetItem("已完成")
                    status_item.setForeground(Qt.GlobalColor.darkGreen)
                else:
                    status_item = QTableWidgetItem("文件丢失")
                    status_item.setForeground(Qt.GlobalColor.red)
            elif status == "failed":
                status_item = QTableWidgetItem("失败")
                status_item.setForeground(Qt.GlobalColor.red)
                status_item.setToolTip(rec.get("error_msg", ""))
            else:
                status_item = QTableWidgetItem(status)
            self._table.setItem(row, 5, status_item)

            # 操作按钮
            btn_widget = self._create_action_buttons(rec)
            self._table.setCellWidget(row, 6, btn_widget)

    def _create_action_buttons(self, rec: dict) -> QWidget:
        """创建操作按钮组"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        file_path = rec.get("file_path", "")
        record_id = rec.get("id")

        btn_style_green = (
            "QPushButton { background-color: #EAF7F0; color: #1F9254; "
            "border: 1px solid #BFE6D2; border-radius: 6px; font-size: 12px; "
            "font-weight: 500; padding: 2px 10px; }"
            "QPushButton:hover { background-color: #D8F0E4; }")
        btn_style_red = (
            "QPushButton { background-color: #FDECEC; color: #D9363E; "
            "border: 1px solid #F7C6C7; border-radius: 6px; font-size: 12px; "
            "font-weight: 500; padding: 2px 10px; }"
            "QPushButton:hover { background-color: #FBD9D9; }")

        # 打开目录按钮（仅文件存在时可用）
        if file_path and os.path.exists(file_path):
            open_btn = QPushButton("打开")
            open_btn.setFixedHeight(26)
            open_btn.setStyleSheet(btn_style_green)
            open_btn.clicked.connect(
                lambda _, p=file_path: self._open_folder(p))
            layout.addWidget(open_btn)

        # 删除按钮（弹窗确认：删除记录 or 同时删除文件）
        del_btn = QPushButton("删除")
        del_btn.setFixedHeight(26)
        del_btn.setStyleSheet(btn_style_red)
        del_btn.clicked.connect(
            lambda _, rid=record_id, fp=file_path: self._on_delete_confirm(rid, fp))
        layout.addWidget(del_btn)

        return container

    # ---- 事件处理 ----

    def _on_search(self):
        self._search_keyword = self._search_input.text().strip()
        self._current_page = 0
        self.refresh()

    def _on_platform_changed(self):
        self._current_platform = self._platform_combo.currentData()
        self._search_keyword = ""
        self._search_input.clear()
        self._current_page = 0
        self.refresh()

    def _on_prev_page(self):
        if self._current_page > 0:
            self._current_page -= 1
            self.refresh()

    def _on_next_page(self):
        self._current_page += 1
        self.refresh()

    def _on_delete(self, record_id: int):
        self._db.delete_record(record_id)
        self.refresh()

    def _on_delete_confirm(self, record_id: int, file_path: str):
        """删除确认弹窗：区分仅删除记录 / 同时删除文件"""
        file_exists = bool(file_path) and os.path.exists(file_path)

        if file_exists:
            msg = QMessageBox(self)
            msg.setWindowTitle("确认删除")
            msg.setText("请选择删除方式：")
            msg.setInformativeText(f"文件路径：{file_path}")
            record_only_btn = msg.addButton(
                "仅删除记录", QMessageBox.ButtonRole.AcceptRole)
            delete_file_btn = msg.addButton(
                "同时删除文件", QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            msg.exec()

            clicked = msg.clickedButton()
            if clicked == record_only_btn:
                self._db.delete_record(record_id)
                self.refresh()
            elif clicked == delete_file_btn:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                self._db.delete_record(record_id)
                self.refresh()
        else:
            # 文件不存在，直接删除记录
            self._db.delete_record(record_id)
            self.refresh()

    def _open_folder(self, file_path: str):
        """打开文件所在目录并选中文件"""
        if not os.path.exists(file_path):
            return
        if sys.platform == "win32":
            subprocess.Popen(
                ["explorer", "/select,", os.path.normpath(file_path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(file_path)])
