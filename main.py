"""
视频下载工具 - GUI 入口
"""
import os
import sys
import io

# 修复 Windows GBK 控制台编码问题。
# 注意：打包为 --noconsole 时 sys.stdout/stderr 可能为 None，或被日志系统
# 替换为无 buffer 属性的对象，因此这里必须做防御性判断，否则启动即崩溃。
if sys.platform == "win32":
    if getattr(sys.stdout, "buffer", None) is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if getattr(sys.stderr, "buffer", None) is not None:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel,
    QFileDialog, QInputDialog, QMessageBox, QFrame, QDialog,
    QComboBox, QTabWidget, QPlainTextEdit, QCheckBox,
    QSystemTrayIcon, QStyle,
)
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QPixmap, QShortcut, QKeySequence, QIcon

from core.extractor_factory import ExtractorFactory
from core.environment_checker import EnvironmentChecker
from core.download_manager import DownloadManager
from core.logger import setup_logging, get_log_dir
from core.cookie_utils import check_cookie_status
from core.cookie_manager import BilibiliCookieManager, DouyinCookieManager
from core.database import DownloadHistoryDB
from core.task_queue import TaskQueue, DownloadTask
from gui.task_worker import TaskWorker
from gui.task_list_widget import TaskListWidget
from gui.history_widget import HistoryWidget
from gui.cookie_dialog import CookieDialog

# ---- QSS 样式 ----
APP_STYLE = """
/* ===== 全局 ===== */
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", "Helvetica Neue", sans-serif;
    font-size: 13px;
}
QMainWindow, QWidget { background-color: #F6F7F9; color: #1F2329; }
QLabel { background: transparent; }
QToolTip {
    background-color: #1F2329; color: #FFFFFF; border: none;
    border-radius: 6px; padding: 6px 10px; font-size: 12px;
}

/* ===== 菜单栏 ===== */
QMenuBar { background-color: #FFFFFF; border-bottom: 1px solid #EDEFF3; padding: 4px 6px; }
QMenuBar::item { padding: 6px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background-color: #FDEFF4; color: #D44A74; }
QMenu { background-color: #FFFFFF; border: 1px solid #EDEFF3; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 7px 28px 7px 14px; border-radius: 6px; }
QMenu::item:selected { background-color: #FDEFF4; color: #D44A74; }
QMenu::separator { height: 1px; background-color: #EDEFF3; margin: 6px 10px; }

/* ===== 输入框 ===== */
QLineEdit, QPlainTextEdit {
    border: 1px solid #E1E4EA; border-radius: 8px; padding: 9px 12px;
    background-color: #FFFFFF; selection-background-color: #FBCFE0; selection-color: #1F2329;
}
QLineEdit:hover, QPlainTextEdit:hover { border-color: #D6DAE1; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #FB7299; }

/* ===== 按钮（主色） ===== */
QPushButton {
    background-color: #FB7299; color: #FFFFFF; border: none;
    border-radius: 8px; padding: 9px 20px; font-size: 13px; font-weight: 500;
}
QPushButton:hover { background-color: #EA5C87; }
QPushButton:pressed { background-color: #D44A74; }
QPushButton:disabled { background-color: #F0F1F3; color: #B4B9C0; }

/* ===== 下拉框 ===== */
QComboBox {
    border: 1px solid #E1E4EA; border-radius: 8px; padding: 7px 12px;
    background-color: #FFFFFF; min-height: 18px;
}
QComboBox:hover { border-color: #D6DAE1; }
QComboBox:focus { border-color: #FB7299; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView {
    border: 1px solid #EDEFF3; border-radius: 8px; background-color: #FFFFFF;
    selection-background-color: #FDEFF4; selection-color: #D44A74; padding: 4px; outline: none;
}

/* ===== 卡片 ===== */
QFrame#info_card {
    background-color: #FFFFFF; border-radius: 12px; border: 1px solid #EDEFF3;
}

/* ===== 表格 ===== */
QTableWidget {
    background-color: #FFFFFF; border: 1px solid #EDEFF3; border-radius: 10px;
    gridline-color: transparent; outline: none; alternate-background-color: #FBFCFD;
    selection-background-color: #FDEFF4; selection-color: #1F2329;
}
QTableWidget::item { padding: 7px 8px; border: none; }
QTableWidget::item:selected { background-color: #FDEFF4; color: #1F2329; }
QHeaderView::section {
    background-color: #FFFFFF; color: #8A9099; padding: 9px 8px;
    border: none; border-bottom: 1px solid #EDEFF3; font-weight: 500; font-size: 12px;
}
QTableCornerButton::section { background-color: #FFFFFF; border: none; }

/* ===== 状态栏 ===== */
QStatusBar { background-color: #FFFFFF; border-top: 1px solid #EDEFF3; color: #8A9099; font-size: 12px; }
QStatusBar::item { border: none; }

/* ===== 滚动条 ===== */
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
QScrollBar::handle:vertical { background: #D8DCE2; border-radius: 4px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #C2C7CE; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px 4px; }
QScrollBar::handle:horizontal { background: #D8DCE2; border-radius: 4px; min-width: 32px; }
QScrollBar::handle:horizontal:hover { background: #C2C7CE; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def _format_duration(seconds: int) -> str:
    """将秒数格式化为 mm:ss 或 hh:mm:ss"""
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _friendly_codec(codecs: str) -> str:
    """把编码串（avc1/hev1/av01...）转换成普通用户能看懂的叫法"""
    c = (codecs or "").lower()
    if c.startswith(("avc", "h264")):
        return "H.264"
    if c.startswith(("hev", "hvc", "h265")):
        return "H.265"
    if c.startswith("av01"):
        return "AV1"
    if "mp4a" in c or c.startswith("aac"):
        return "AAC"
    return codecs or ""


def _format_count(n: int) -> str:
    """将数字格式化为易读形式（如 12.3万）"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


class CoverLoader(QThread):
    """
    后台线程加载封面图。
    【修复】旧实现直接在 UI 线程 requests.get(timeout=5)，封面 CDN 慢时
    会把整个窗口冻住最长 5 秒。
    """

    loaded = Signal(bytes)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self):
        try:
            resp = requests.get(self._url, timeout=8)
            resp.raise_for_status()
            self.loaded.emit(resp.content)
        except Exception:
            self.loaded.emit(b"")


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self, factory: ExtractorFactory, env_checker: EnvironmentChecker,
                 db: DownloadHistoryDB):
        super().__init__()
        self._factory = factory
        self._env_checker = env_checker
        self._download_manager = DownloadManager(factory, env_checker)
        self._db = db
        self._current_video_info = None
        self._current_url = ""  # 当前解析链接（已规范化，用于入队与历史记录）
        self._cover_token = 0   # 封面加载令牌，避免快速连续解析时串图
        self._cover_loader = None
        self._last_clipboard = ""  # 上次处理过的剪贴板内容
        # 当前视频可选能力的显式标记（比依赖控件 isVisible 可靠）
        self._quality_available = False
        self._danmaku_available = False
        self._subtitle_available = False

        # 批量下载队列与并发工作线程
        self._task_queue = TaskQueue()
        self._active_workers: list[TaskWorker] = []

        self._tray = None

        self.setWindowTitle("视频下载工具")
        self.resize(1160, 780)
        self._init_menu()
        self._init_ui()
        self._init_tray()

        # 状态栏使用永久 QLabel，避免被临时消息覆盖
        self._status_bar_label = QLabel()
        self._status_bar_label.setStyleSheet(
            "color: #8A9099; font-size: 12px; padding: 0 8px; background: transparent;")
        self.statusBar().addPermanentWidget(self._status_bar_label, 1)
        self._update_status_bar()

        # 启动后检查 Cookie 是否已过期/即将过期（延迟到窗口显示后再提示，避免卡启动）
        QTimer.singleShot(0, self._warn_stale_cookies)

    def _init_menu(self):
        """初始化菜单栏（动态生成 Cookie 菜单）"""
        menu_bar = self.menuBar()
        settings_menu = menu_bar.addMenu("设置")

        ffmpeg_action = settings_menu.addAction("指定 FFmpeg 路径")
        ffmpeg_action.triggered.connect(self._on_set_ffmpeg_path)

        save_dir_action = settings_menu.addAction("指定存储目录")
        save_dir_action.triggered.connect(self._on_set_save_dir)

        concurrent_action = settings_menu.addAction("最大并发下载数")
        concurrent_action.triggered.connect(self._on_set_max_concurrent)

        settings_menu.addSeparator()

        self._auto_paste_action = settings_menu.addAction("复制链接后自动填入")
        self._auto_paste_action.setCheckable(True)
        self._auto_paste_action.setChecked(self._env_checker.get_auto_paste())
        self._auto_paste_action.toggled.connect(self._env_checker.set_auto_paste)

        conn_action = settings_menu.addAction("下载并发（线程数）")
        conn_action.triggered.connect(self._on_set_connections)

        template_action = settings_menu.addAction("文件名模板")
        template_action.triggered.connect(self._on_set_filename_template)

        log_action = settings_menu.addAction("打开日志目录")
        log_action.triggered.connect(self._on_open_log_dir)

        settings_menu.addSeparator()

        # 动态为每个需要 Cookie 的插件生成菜单项（统一打开 Cookie 设置对话框）
        platforms = self._factory.get_registered_platforms()
        for platform in platforms:
            if not platform["requires_cookie"]:
                continue

            platform_id = platform["platform_id"]
            platform_name = platform["platform_name"]

            action = settings_menu.addAction(f"{platform_name} Cookie")
            action.triggered.connect(
                lambda checked, pid=platform_id, pname=platform_name:
                    self._on_set_cookie_dialog(pid, pname)
            )

    @staticmethod
    def _get_login_manager(platform_id: str):
        """返回平台对应的扫码登录管理器类（不支持扫码登录则返回 None）"""
        if platform_id == "bilibili":
            return BilibiliCookieManager
        if platform_id == "douyin":
            return DouyinCookieManager
        return None

    def _init_ui(self):
        """初始化界面布局（Tab 页签结构）"""
        # 顶层 Tab 容器
        self._tab_widget = QTabWidget()
        self._tab_widget.setStyleSheet("""
            QTabWidget::pane { border: none; background: transparent; }
            QTabBar::tab {
                padding: 9px 26px; font-size: 14px; border: none;
                background: transparent; color: #8A9099; margin-right: 4px;
            }
            QTabBar::tab:hover { color: #646A73; }
            QTabBar::tab:selected {
                color: #FB7299; border-bottom: 2px solid #FB7299; font-weight: 500;
            }
        """)
        self.setCentralWidget(self._tab_widget)

        # 支持把链接/分享文案直接拖进窗口
        self.setAcceptDrops(True)

        # ---- Tab 1: 下载 ----
        download_tab = QWidget()
        layout = QVBoxLayout(download_tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # URL 输入区域
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self._url_input = QPlainTextEdit()
        self._url_input.setPlaceholderText(
            "粘贴视频链接、分享文案或 BV / AV 号，每行一个（支持批量）\n"
            "支持：B站(bilibili.com / b23.tv / BV号) · 抖音(douyin.com / v.douyin.com) · Twitter/X")
        self._url_input.setFixedHeight(88)
        self._parse_btn = QPushButton("解析")
        self._parse_btn.setFixedWidth(84)
        self._parse_btn.setFixedHeight(88)
        self._parse_btn.clicked.connect(self._on_parse)
        input_row.addWidget(self._url_input)
        input_row.addWidget(self._parse_btn)
        layout.addLayout(input_row)

        # 状态提示
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #646A73; font-size: 13px; background: transparent;")
        layout.addWidget(self._status_label)

        # 视频信息卡片（封面 + 文字信息）
        self._info_card = QFrame()
        self._info_card.setObjectName("info_card")
        self._info_card.setVisible(False)
        info_layout = QHBoxLayout(self._info_card)
        info_layout.setContentsMargins(12, 12, 12, 12)
        info_layout.setSpacing(16)

        # 封面图
        self._cover_label = QLabel()
        self._cover_label.setFixedSize(200, 125)
        self._cover_label.setStyleSheet("background-color: #F0F1F3; border-radius: 8px;")
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setScaledContents(True)
        info_layout.addWidget(self._cover_label)

        # 文字信息区
        text_layout = QVBoxLayout()
        text_layout.setSpacing(6)

        self._title_label = QLabel("")
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(
            "font-size: 16px; font-weight: 600; color: #1F2329; background: transparent;")
        text_layout.addWidget(self._title_label)

        self._owner_label = QLabel("")
        self._owner_label.setStyleSheet("font-size: 13px; color: #FB7299; background: transparent;")
        text_layout.addWidget(self._owner_label)

        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("font-size: 13px; color: #8A9099; background: transparent;")
        text_layout.addWidget(self._meta_label)

        text_layout.addStretch()
        info_layout.addLayout(text_layout, 1)
        layout.addWidget(self._info_card)

        # 清晰度选择行
        quality_row = QHBoxLayout()
        quality_row.setSpacing(8)
        self._quality_label = QLabel("清晰度：")
        self._quality_label.setStyleSheet("font-size: 13px; color: #646A73; background: transparent;")
        self._quality_label.setVisible(False)
        self._quality_combo = QComboBox()
        self._quality_combo.setMinimumWidth(280)
        self._quality_combo.setVisible(False)
        quality_row.addWidget(self._quality_label)
        quality_row.addWidget(self._quality_combo)
        quality_row.addStretch()
        layout.addLayout(quality_row)

        # 输出选项行：格式 / 分P / 弹幕 / 字幕
        option_row = QHBoxLayout()
        option_row.setSpacing(8)

        fmt_label = QLabel("输出：")
        fmt_label.setStyleSheet("font-size: 13px; color: #646A73; background: transparent;")
        self._format_combo = QComboBox()
        self._format_combo.addItem("视频 MP4", False)
        self._format_combo.addItem("仅音频 MP3", True)
        self._format_combo.setFixedWidth(130)
        option_row.addWidget(fmt_label)
        option_row.addWidget(self._format_combo)

        self._page_label = QLabel("分P：")
        self._page_label.setStyleSheet("font-size: 13px; color: #646A73; background: transparent;")
        self._page_label.setVisible(False)
        self._page_combo = QComboBox()
        self._page_combo.setMinimumWidth(220)
        self._page_combo.setVisible(False)
        self._page_combo.currentIndexChanged.connect(self._on_page_changed)
        option_row.addWidget(self._page_label)
        option_row.addWidget(self._page_combo)

        option_row.addStretch()

        self._danmaku_check = QCheckBox("弹幕")
        self._subtitle_check = QCheckBox("字幕")
        for cb in (self._danmaku_check, self._subtitle_check):
            cb.setStyleSheet(
                "QCheckBox { font-size: 13px; color: #646A73; background: transparent; }")
            cb.setVisible(False)
            option_row.addWidget(cb)

        layout.addLayout(option_row)

        # 添加到队列按钮
        self._download_btn = QPushButton("添加到下载队列")
        self._download_btn.setEnabled(False)
        self._download_btn.setFixedHeight(40)
        self._download_btn.clicked.connect(self._on_add_to_queue)
        layout.addWidget(self._download_btn)

        # 任务队列列表
        self._task_list_widget = TaskListWidget(self._task_queue, parent=self)
        self._task_list_widget.task_retry.connect(self._on_task_retry)
        self._task_list_widget.task_promote.connect(self._on_task_promote)
        layout.addWidget(self._task_list_widget, 1)

        self._tab_widget.addTab(download_tab, "下载")

        # ---- Tab 2: 历史记录 ----
        self._history_widget = HistoryWidget(self._db, factory=self._factory, parent=self)
        self._tab_widget.addTab(self._history_widget, "历史记录")

        # 切换到历史记录 Tab 时自动刷新
        self._tab_widget.currentChanged.connect(self._on_tab_changed)

        # ---- 全局交互（需在所有控件创建完成后再绑定）----
        # 快捷键：Ctrl+Enter 解析、Ctrl+L 清空输入
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._on_parse)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self._url_input.clear)
        # 剪贴板监听：复制链接后自动填入
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

    def _on_tab_changed(self, index: int):
        """Tab 切换时，如果切到历史记录则刷新"""
        if index == 1:
            self._history_widget.refresh()

    # ---- 状态栏 ----

    def _update_status_bar(self):
        """更新底部状态栏（动态检查所有平台 Cookie 状态）"""
        parts = []

        # FFmpeg 状态
        if self._env_checker.get_ffmpeg_path():
            parts.append("FFmpeg 已就绪")
        else:
            parts.append("FFmpeg 不可用")

        # 动态检查所有平台的 Cookie 状态（含有效期）
        for platform in self._factory.get_registered_platforms():
            if not platform["requires_cookie"]:
                continue
            platform_id = platform["platform_id"]
            cookie = self._env_checker.get_cookie_for_platform(platform_id)
            state = check_cookie_status(platform_id, cookie)
            parts.append(f"{platform['platform_name']} Cookie {state['hint']}")

        # 存储目录和并发数
        save_dir = self._env_checker.get_save_dir()
        if save_dir:
            parts.append(f"存储: {save_dir}")
        parts.append(f"并发: {self._env_checker.get_max_concurrent()}")

        self._status_bar_label.setText("  |  ".join(parts))

    # ---- 设置相关 ----

    def _on_set_ffmpeg_path(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 FFmpeg 可执行文件", "", "可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*)")
        if path:
            self._env_checker.set_ffmpeg_path(path)
            self._update_status_bar()

    def _on_set_save_dir(self):
        current = self._env_checker.get_save_dir()
        path = QFileDialog.getExistingDirectory(self, "选择存储目录", current)
        if path:
            self._env_checker.set_save_dir(path)
            self._update_status_bar()

    def _on_set_connections(self):
        """设置单文件下载的并发：自动自适应（推荐）或固定线程数"""
        auto = self._env_checker.is_connections_auto()
        current = self._env_checker.get_download_connections()

        options = [
            "自动（推荐，按实测速度自适应）",
            "单线程（1）",
            "固定 2 线程",
            "固定 4 线程",
            "固定 8 线程",
            "固定 16 线程",
        ]
        if auto:
            index = 0
        else:
            index = {1: 1, 2: 2, 4: 3, 8: 4, 16: 5}.get(current, 0)

        choice, ok = QInputDialog.getItem(
            self, "下载并发（线程数）",
            "单个文件同时下载的分片数：\n\n"
            "「自动」：从 4 条连接起步，按实测下载速度自动增减 ——\n"
            "提速明显就继续加，到瓶颈就停，通常是最优选择。\n\n"
            "若某个站点对多连接不友好，可改为固定值或单线程。",
            options, index, False)
        if not ok:
            return

        if choice.startswith("自动"):
            self._env_checker.set_download_connections("auto")
        elif choice.startswith("单线程"):
            self._env_checker.set_download_connections(1)
        else:
            mapping = {"固定 2 线程": 2, "固定 4 线程": 4,
                       "固定 8 线程": 8, "固定 16 线程": 16}
            self._env_checker.set_download_connections(mapping.get(choice, 4))

        if self._env_checker.is_connections_auto():
            self._status_label.setText("下载并发：自动（按速度自适应）")
        else:
            self._status_label.setText(
                f"下载并发：固定 {self._env_checker.get_download_connections()} 线程")

    def _on_set_filename_template(self):
        """设置保存文件的命名模板"""
        current = self._env_checker.get_filename_template()
        text, ok = QInputDialog.getText(
            self, "文件名模板",
            "可用占位符：\n"
            "{title} 视频标题　{owner} UP主/作者\n"
            "{platform} 平台　{quality} 清晰度　{date} 日期\n\n"
            "文件名模板：",
            text=current)
        if ok:
            self._env_checker.set_filename_template(text)
            self._status_label.setText(
                f"文件名模板已保存：{self._env_checker.get_filename_template()}")

    def _on_open_log_dir(self):
        """打开日志目录，方便用户把日志发给开发者排查问题"""
        path = get_log_dir()
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)
            else:
                import subprocess as _sp
                _sp.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.information(
                self, "日志目录", f"日志目录：\n{path}\n\n（自动打开失败：{e}）")

    def _on_set_max_concurrent(self):
        current = self._env_checker.get_max_concurrent()
        val, ok = QInputDialog.getInt(
            self, "设置并发数", "最大同时下载任务数（1-5）：",
            current, 1, 5)
        if ok:
            self._env_checker.set_max_concurrent(val)
            self._update_status_bar()
            # 调整后可能可以启动更多任务
            self._try_start_next()

    # ---- Cookie 设置 ----

    def _warn_stale_cookies(self):
        """
        启动时检查各平台 Cookie 的有效期。
        过期 / 即将过期时给出明确提示，并提供「立即重新配置」入口。
        """
        problems = []
        for platform in self._factory.get_registered_platforms():
            if not platform["requires_cookie"]:
                continue
            pid = platform["platform_id"]
            cookie = self._env_checker.get_cookie_for_platform(pid)
            state = check_cookie_status(pid, cookie)
            if state["state"] in ("expired", "expiring"):
                problems.append((platform["platform_name"], pid, state))

        if not problems:
            return

        self._status_label.setText(
            "⚠ 检测到 Cookie 已过期或即将过期，建议重新配置（设置 → 对应平台 Cookie）")

        lines = "\n".join(f"· {name}：{state['hint']}" for name, _, state in problems)
        reply = QMessageBox.warning(
            self, "Cookie 已过期",
            f"检测到以下 Cookie 已过期或即将过期：\n\n{lines}\n\n"
            "Cookie 过期会导致对应平台解析失败、画质受限或视频带水印。\n\n"
            "是否现在重新配置？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if reply == QMessageBox.StandardButton.Yes:
            platform_name, platform_id, _ = problems[0]
            self._on_set_cookie_dialog(platform_id, platform_name)

    def _on_set_cookie_dialog(self, platform_id: str, platform_name: str):
        """
        打开 Cookie 设置对话框：
        支持「一键扫码登录」与「整段粘贴自动识别」，全程有步骤提示与实时校验，
        面向完全不熟悉 Cookie 的用户。
        """
        current_cookie = self._env_checker.get_cookie_for_platform(platform_id)
        dlg = CookieDialog(
            platform_id, platform_name,
            current_cookie=current_cookie,
            login_manager_cls=self._get_login_manager(platform_id),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        cookie = dlg.get_cookie()
        self._env_checker.set_cookie_for_platform(platform_id, cookie)
        self._status_label.setText(
            f"{platform_name} Cookie 已保存（已自动提取关键字段）"
            if cookie else f"{platform_name} Cookie 已清除")
        self._update_status_bar()

    # ---- 解析 ----

    def _on_parse(self):
        """解析入口：根据输入行数自动选择单个或批量模式"""
        text = self._url_input.toPlainText().strip()
        if not text:
            self._status_label.setText("请输入视频链接")
            return

        urls = [u.strip() for u in text.split('\n') if u.strip()]

        if len(urls) == 1:
            self._parse_single_url(urls[0])
        else:
            self._parse_batch_urls(urls)

    def _parse_single_url(self, url: str):
        """单个 URL 解析：显示信息卡片，供用户选择清晰度后手动添加队列"""
        self._status_label.setText("正在解析...")
        self._info_card.setVisible(False)
        self._download_btn.setEnabled(False)
        self._quality_combo.setVisible(False)
        self._quality_label.setVisible(False)
        self._quality_available = False
        self._page_combo.setVisible(False)
        self._page_label.setVisible(False)
        self._danmaku_check.setVisible(False)
        self._subtitle_check.setVisible(False)
        self._danmaku_available = False
        self._subtitle_available = False
        self._parse_btn.setEnabled(False)

        # 规范化链接（支持分享文案 / 短链还原 / BV·AV 号补全），便于后续存储与复用
        normalized_url = self._factory.normalize_url(url)
        self._current_url = normalized_url

        extractor = self._factory.get_extractor(normalized_url)
        if extractor is None:
            self._status_label.setText("不支持的链接")
            self._parse_btn.setEnabled(True)
            return

        # 根据插件的 PLATFORM_ID 自动获取对应的 Cookie
        cookie = ""
        if extractor.REQUIRES_COOKIE:
            cookie = self._env_checker.get_cookie_for_platform(extractor.PLATFORM_ID)

        # 在后台线程中执行解析（抖音 Selenium 渲染耗时较长）
        class ParseWorker(QThread):
            success = Signal(dict)
            error = Signal(str)
            not_implemented = Signal()

            def __init__(self, ext, url, cookie, parent=None):
                super().__init__(parent)
                self._ext = ext
                self._url = url
                self._cookie = cookie

            def run(self):
                try:
                    info = self._ext.extract_info(self._url, cookie=self._cookie)
                    self.success.emit(info)
                except NotImplementedError:
                    self.not_implemented.emit()
                except Exception as e:
                    self.error.emit(str(e))

        def on_parse_success(video_info):
            self._parse_btn.setEnabled(True)
            self._current_video_info = video_info
            self._show_video_info(video_info)

        def on_parse_error(msg):
            self._parse_btn.setEnabled(True)
            self._status_label.setText(f"解析失败: {msg}")

        def on_not_implemented():
            self._parse_btn.setEnabled(True)
            QMessageBox.information(self, "功能开发中", "该功能开发中，敬请期待")
            self._status_label.setText("")

        worker = ParseWorker(extractor, normalized_url, cookie)
        worker.success.connect(on_parse_success)
        worker.error.connect(on_parse_error)
        worker.not_implemented.connect(on_not_implemented)
        worker.start()
        self._parse_worker = worker

    def _parse_batch_urls(self, urls: list[str]):
        """
        多个 URL 批量解析：依次解析并自动添加到下载队列。
        使用单个后台线程串行解析所有 URL，通过信号逐条汇报结果。
        """
        save_dir = self._env_checker.get_save_dir()
        if not save_dir:
            save_dir = QFileDialog.getExistingDirectory(self, "请先选择保存目录")
            if not save_dir:
                return
            self._env_checker.set_save_dir(save_dir)
            self._update_status_bar()

        self._info_card.setVisible(False)
        self._download_btn.setEnabled(False)
        self._quality_combo.setVisible(False)
        self._quality_label.setVisible(False)
        self._quality_available = False
        self._page_combo.setVisible(False)
        self._page_label.setVisible(False)
        self._danmaku_check.setVisible(False)
        self._subtitle_check.setVisible(False)
        self._danmaku_available = False
        self._subtitle_available = False
        self._parse_btn.setEnabled(False)
        self._status_label.setText(f"正在批量解析 0/{len(urls)} ...")

        class BatchParseWorker(QThread):
            """后台线程：串行解析多个 URL"""
            one_done = Signal(str, dict)    # (url, video_info)
            one_error = Signal(str, str)    # (url, error_msg)
            progress = Signal(int, int)     # (current, total)
            all_done = Signal()

            def __init__(self, urls, factory, env_checker, parent=None):
                super().__init__(parent)
                self._urls = urls
                self._factory = factory
                self._env = env_checker

            def run(self):
                for i, url in enumerate(self._urls):
                    self.progress.emit(i + 1, len(self._urls))
                    try:
                        # 规范化链接（分享文案 / 短链 / BV·AV 号均可）
                        norm_url = self._factory.normalize_url(url)
                        extractor = self._factory.get_extractor(norm_url)
                        if extractor is None:
                            self.one_error.emit(url, "不支持的链接")
                            continue
                        # 根据插件的 PLATFORM_ID 自动获取对应的 Cookie
                        cookie = ""
                        if extractor.REQUIRES_COOKIE:
                            cookie = self._env.get_cookie_for_platform(extractor.PLATFORM_ID)
                        info = extractor.extract_info(norm_url, cookie=cookie)
                        self.one_done.emit(norm_url, info)
                    except Exception as e:
                        self.one_error.emit(url, str(e))
                self.all_done.emit()

        success_count = 0
        fail_count = 0

        def on_one_done(url, video_info):
            nonlocal success_count
            success_count += 1
            task = DownloadTask(
                url=url,
                video_info=video_info,
                quality_id=None,
                save_dir=save_dir,
            )
            self._task_queue.add(task)
            self._task_list_widget.refresh()
            self._try_start_next()

        def on_one_error(url, error_msg):
            nonlocal fail_count
            fail_count += 1
            print(f"  [批量] 解析失败: {url} -> {error_msg}")

        def on_progress(current, total):
            self._status_label.setText(f"正在批量解析 {current}/{total} ...")

        def on_all_done():
            self._parse_btn.setEnabled(True)
            self._url_input.clear()
            self._status_label.setText(
                f"批量解析完成：成功 {success_count} 个，失败 {fail_count} 个")

        worker = BatchParseWorker(urls, self._factory, self._env_checker)
        worker.one_done.connect(on_one_done)
        worker.one_error.connect(on_one_error)
        worker.progress.connect(on_progress)
        worker.all_done.connect(on_all_done)
        worker.start()
        self._batch_parse_worker = worker

    def _show_video_info(self, info: dict):
        """展示视频信息卡片"""
        title = info.get("title", "")
        if not title:
            self._status_label.setText("解析完成，但未获取到视频标题")
            return

        self._title_label.setText(title)
        self._owner_label.setText(f"UP主: {info.get('owner', '未知')}")

        # 时长 + 播放量 + 弹幕数
        meta_parts = []
        duration = info.get("duration", 0)
        if duration:
            meta_parts.append(f"时长 {_format_duration(duration)}")
        view_count = info.get("view_count", 0)
        if view_count:
            meta_parts.append(f"播放 {_format_count(view_count)}")
        danmaku = info.get("danmaku_count", 0)
        if danmaku:
            meta_parts.append(f"弹幕 {_format_count(danmaku)}")
        self._meta_label.setText("  |  ".join(meta_parts))

        # 加载封面图（后台线程，避免网络慢时卡住整个界面）
        self._cover_token += 1
        token = self._cover_token
        cover_url = info.get("cover_url", "")
        self._cover_label.setPixmap(QPixmap())
        if cover_url:
            self._cover_label.setText("封面加载中…")
            loader = CoverLoader(cover_url)
            loader.loaded.connect(lambda data, t=token: self._on_cover_loaded(t, data))
            loader.start()
            self._cover_loader = loader  # 保持引用避免被回收
        else:
            self._cover_label.setText("")

        self._info_card.setVisible(True)
        self._download_btn.setEnabled(True)

        # 填充清晰度下拉框（编码名转成人类可读，最高画质标注「推荐」）
        self._quality_combo.clear()
        streams = info.get("streams", [])
        video_streams = [s for s in streams if s.get("type") == "video"]
        if video_streams:
            seen = set()
            first = True
            for s in video_streams:
                qid = s["quality_id"]
                if qid in seen:
                    continue
                seen.add(qid)
                codec = _friendly_codec(s.get("codecs", ""))
                kbps = s.get("bandwidth", 0) // 1000
                parts = [s["quality_name"]]
                if codec:
                    parts.append(codec)
                if kbps:
                    parts.append(f"{kbps}kbps")
                label = " · ".join(parts)
                if first:
                    label += "　（推荐）"
                    first = False
                self._quality_combo.addItem(label, qid)
            self._quality_combo.setVisible(True)
            self._quality_label.setVisible(True)
            self._quality_available = True
        else:
            self._quality_combo.setVisible(False)
            self._quality_label.setVisible(False)
            self._quality_available = False

        # 填充分P下拉框（B站多P视频）
        pages = info.get("pages") or []
        self._page_combo.blockSignals(True)
        self._page_combo.clear()
        if len(pages) > 1:
            current_cid = info.get("cid")
            for i, pg in enumerate(pages):
                self._page_combo.addItem(f"P{pg['index']}　{pg['title']}", pg["index"])
                if pg.get("cid") == current_cid:
                    self._page_combo.setCurrentIndex(i)
            self._page_label.setVisible(True)
            self._page_combo.setVisible(True)
        else:
            self._page_label.setVisible(False)
            self._page_combo.setVisible(False)
        self._page_combo.blockSignals(False)

        # 弹幕 / 字幕仅 B站支持
        is_bilibili = info.get("platform") == "bilibili"
        self._danmaku_available = is_bilibili
        self._subtitle_available = is_bilibili
        self._danmaku_check.setVisible(is_bilibili)
        self._subtitle_check.setVisible(is_bilibili)
        self._danmaku_check.setChecked(False)
        self._subtitle_check.setChecked(False)

        platform = info.get("platform", "")
        # 动态检查当前平台的 Cookie 状态（含有效期）
        cookie = self._env_checker.get_cookie_for_platform(platform)
        cookie_state = check_cookie_status(platform, cookie)

        # 根据平台查找对应的插件元数据
        platform_info = next((p for p in self._factory.get_registered_platforms()
                              if p["platform_id"] == platform), None)
        platform_name = platform_info["platform_name"] if platform_info else platform

        if platform_info and platform_info["requires_cookie"]:
            if cookie_state["state"] == "missing":
                self._status_label.setText(
                    f"解析成功（未配置 {platform_name} Cookie，可能无法获取高清或无水印视频）")
            elif cookie_state["state"] == "expired":
                self._status_label.setText(
                    f"⚠ 解析成功，但 {platform_name} Cookie 已过期，下载可能失败"
                    f"（设置 → {platform_name} Cookie 重新配置）")
            elif cookie_state["state"] == "expiring":
                self._status_label.setText(
                    f"解析成功（{platform_name} Cookie {cookie_state['hint']}，建议尽快重新配置）")
            else:
                self._status_label.setText("解析成功")
        else:
            self._status_label.setText("解析成功")

    def _on_cover_loaded(self, token: int, data: bytes):
        """封面后台加载完成（仅当仍是最新一次解析时应用，避免串图）"""
        if token != self._cover_token:
            return
        if not data:
            self._cover_label.setText("封面加载失败")
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self._cover_label.setPixmap(pixmap)
        else:
            self._cover_label.setText("封面加载失败")

    def _on_page_changed(self, index: int):
        """分P切换：重新按新的 ?p=N 解析，让清晰度与流地址对应到该分P"""
        if index < 0:
            return
        page = self._page_combo.itemData(index)
        if page is None:
            return

        base_url = self._current_url or self._url_input.toPlainText().strip()
        if not base_url:
            return

        parts = urlsplit(base_url)
        query = dict(parse_qsl(parts.query))
        query["p"] = str(page)
        new_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                              urlencode(query), parts.fragment))
        self._url_input.setPlainText(new_url)
        self._parse_single_url(new_url)

    # ---- 批量下载队列 ----

    def _on_add_to_queue(self):
        """将当前解析结果添加到下载队列"""
        if self._current_video_info is None:
            return

        save_dir = self._env_checker.get_save_dir()
        if not save_dir:
            save_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
            if not save_dir:
                return
            self._env_checker.set_save_dir(save_dir)
            self._update_status_bar()

        # 优先使用解析阶段的规范化链接（分享文案会被还原为真实 URL）
        url = self._current_url or self._url_input.toPlainText().strip()

        # 重复下载提示：数据库里已有成功记录时先确认
        existing = self._db.find_completed(url, self._current_video_info.get("title", ""))
        if existing:
            reply = QMessageBox.question(
                self, "可能重复下载",
                f"该视频似乎已经下载过：\n{existing.get('title', '')}\n"
                f"（{existing.get('created_at', '')}）\n\n仍要再下载一份吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        audio_only = bool(self._format_combo.currentData())
        # 仅音频时无需选清晰度
        quality_id = None
        if not audio_only and self._quality_available:
            quality_id = self._quality_combo.currentData()

        task = DownloadTask(
            url=url,
            video_info=self._current_video_info,
            quality_id=quality_id,
            save_dir=save_dir,
            audio_only=audio_only,
            want_danmaku=self._danmaku_available and self._danmaku_check.isChecked(),
            want_subtitle=self._subtitle_available and self._subtitle_check.isChecked(),
        )
        self._task_queue.add(task)
        self._task_list_widget.refresh()
        self._status_label.setText(f"已添加到队列: {task.title}")

        # 清空输入区域，方便继续输入下一个链接
        self._url_input.clear()
        self._info_card.setVisible(False)
        self._quality_combo.setVisible(False)
        self._quality_label.setVisible(False)
        self._download_btn.setEnabled(False)
        self._current_video_info = None
        self._current_url = ""

        # 尝试启动下载
        self._try_start_next()

    def _try_start_next(self):
        """尝试从队列取出下一个任务并启动（遵守并发数限制）"""
        max_concurrent = self._env_checker.get_max_concurrent()
        while len(self._active_workers) < max_concurrent:
            task = self._task_queue.get_next_pending()
            if task is None:
                break
            worker = TaskWorker(task, self._download_manager, parent=self)
            worker.progress.connect(self._on_task_progress)
            worker.finished_ok.connect(self._on_task_finished)
            worker.error.connect(self._on_task_error)
            worker.cancelled.connect(self._on_task_cancelled)
            worker.finished.connect(lambda _=None, w=worker: self._on_worker_done(w))
            self._active_workers.append(worker)
            self._task_list_widget.refresh()
            worker.start()

    def _on_task_progress(self, task_id: str, downloaded: int, total: int,
                          speed: float = 0.0, eta: int = -1):
        """更新指定任务的进度（含速度与剩余时间）"""
        self._task_list_widget.update_progress(task_id, downloaded, total, speed, eta)

    def _on_task_finished(self, task_id: str, file_path: str):
        """任务下载完成，写入数据库记录"""
        task = self._task_queue.get_task(task_id)
        self._task_list_widget.mark_completed(task_id)

        if task:
            self._notify("下载完成", task.title)

        if task:
            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            self._db.add_record({
                "url": task.url,
                "title": task.title,
                "platform": task.platform,
                "owner": task.video_info.get("owner", ""),
                "duration": task.video_info.get("duration", 0),
                "quality_name": task.quality_name,
                "quality_id": task.quality_id or 0,
                "file_path": file_path,
                "file_size": file_size,
                "cover_url": task.video_info.get("cover_url", ""),
                "status": "completed",
            })

    def _on_task_error(self, task_id: str, error_msg: str):
        """任务下载失败，写入数据库记录"""
        task = self._task_queue.get_task(task_id)
        self._task_list_widget.mark_failed(task_id)

        if task:
            self._db.add_record({
                "url": task.url,
                "title": task.title,
                "platform": task.platform,
                "owner": task.video_info.get("owner", ""),
                "duration": task.video_info.get("duration", 0),
                "quality_name": task.quality_name,
                "quality_id": task.quality_id or 0,
                "file_path": "",
                "file_size": 0,
                "cover_url": task.video_info.get("cover_url", ""),
                "status": "failed",
                "error_msg": error_msg,
            })

        # 检测是否为清晰度/Cookie 相关问题
        lower_msg = error_msg.lower()
        if "403" in lower_msg or "forbidden" in lower_msg:
            hint = "可能原因：Cookie 未配置或已过期，或当前账号无此清晰度权限。"
            QMessageBox.warning(self, "下载失败", f"{error_msg}\n\n{hint}")

    def _on_task_cancelled(self, task_id: str):
        """任务被用户取消"""
        self._task_list_widget.mark_cancelled(task_id)
        self._status_label.setText("已取消该下载任务")

    def _on_task_retry(self, task_id: str):
        """
        失败/已取消的任务重新排队。

        【重要】必须把它置顶后再调度：get_next_pending() 取的是队列里第一个
        pending 任务，若不置顶，用户点「重试」实际启动的会是排在最前面的
        其它等待任务，看起来就像「所有没下完的都一起重试了」。
        """
        if self._task_queue.reset_for_retry(task_id):
            self._task_queue.promote(task_id)  # 置顶，确保先下用户点的这个
            task = self._task_queue.get_task(task_id)
            self._task_list_widget.refresh()
            self._status_label.setText(f"重新排队：{task.title if task else ''}")
            self._try_start_next()

    def _on_task_promote(self, task_id: str):
        """把待下载任务置顶"""
        if self._task_queue.promote(task_id):
            self._task_list_widget.refresh()
            self._status_label.setText("已置顶该任务")

    def _on_worker_done(self, worker: TaskWorker):
        """工作线程结束后清理并尝试启动下一个任务"""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        self._try_start_next()

    # ---- 拖拽 / 剪贴板 ----

    def dragEnterEvent(self, event):
        """允许拖入文本或链接"""
        mime = event.mimeData()
        if mime.hasText() or mime.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """把拖入的链接/文案填入输入框"""
        mime = event.mimeData()
        text = (mime.text() or "").strip()
        if not text and mime.hasUrls():
            text = "\n".join(u.toString() for u in mime.urls())
        if text:
            self._url_input.setPlainText(text)
            self._status_label.setText("已填入拖入的链接，按 Ctrl+Enter 开始解析")
            event.acceptProposedAction()

    def _on_clipboard_changed(self):
        """
        剪贴板变化时，若主输入框为空、窗口处于激活状态且内容是受支持的链接，
        自动填入输入框（避免打扰：仅在输入框为空时生效，可在设置中关闭）。
        """
        if not self._env_checker.get_auto_paste():
            return
        if not self.isActiveWindow():
            return
        if self._url_input.toPlainText().strip():
            return

        try:
            text = QApplication.clipboard().text().strip()
        except Exception:
            return

        if not text or text == self._last_clipboard or len(text) > 2000:
            return
        self._last_clipboard = text

        if not self._factory.is_supported_text(text):
            return

        self._url_input.setPlainText(text)
        self._status_label.setText("已从剪贴板自动填入链接，按 Ctrl+Enter 开始解析")

    def _init_tray(self):
        """初始化托盘图标（用于下载完成提示）；系统不支持时静默跳过"""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                self._tray = None
                return
            self._tray = QSystemTrayIcon(self._app_icon(), self)
            self._tray.setToolTip("视频下载工具")
            self._tray.show()
        except Exception:
            self._tray = None

    def _app_icon(self) -> QIcon:
        """定位程序图标（兼容开发目录与打包后的 _MEIPASS）"""
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base, "icon.ico")
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)

    def _notify(self, title: str, message: str):
        """下载完成提示：优先托盘气泡；无托盘则播放系统提示音"""
        try:
            if self._tray is not None:
                if not self.isActiveWindow():
                    self._tray.showMessage(
                        title, message,
                        QSystemTrayIcon.MessageIcon.Information, 5000)
            else:
                QApplication.beep()
        except Exception:
            pass

    def _close_plugin_resources(self):
        """释放插件持有的常驻资源（如抖音的常驻浏览器）"""
        seen = set()
        for cls in self._factory._registry.values():
            if cls not in seen:
                seen.add(cls)
                if hasattr(cls, "close_browser"):
                    try:
                        cls.close_browser()
                    except Exception:
                        pass

    def closeEvent(self, event):
        """窗口关闭：若有任务正在下载先确认；随后清理常驻资源"""
        running = [t for t in self._task_queue.all_tasks() if t.status == "downloading"]
        if running:
            reply = QMessageBox.question(
                self, "仍有下载进行中",
                f"还有 {len(running)} 个任务正在下载。\n\n"
                "「是」＝ 取消下载并退出\n「否」＝ 继续下载（不退出）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            for task in running:
                task.cancel()
            for worker in list(self._active_workers):
                worker.wait(3000)

        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception:
            pass

        self._close_plugin_resources()
        super().closeEvent(event)


def main():
    # 初始化日志（打包后无控制台，日志落盘便于排查问题）
    setup_logging()

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)

    factory = ExtractorFactory()
    factory.scan_plugins()

    env_checker = EnvironmentChecker()
    env_checker.check_ffmpeg()

    db = DownloadHistoryDB()

    window = MainWindow(factory, env_checker, db)
    window.show()

    # 首次启动时如果没有配置存储目录，弹窗让用户选择
    if not env_checker.get_save_dir():
        save_dir = QFileDialog.getExistingDirectory(window, "请选择视频存储目录")
        if save_dir:
            env_checker.set_save_dir(save_dir)
            window._update_status_bar()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
