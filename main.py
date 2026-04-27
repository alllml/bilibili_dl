"""
视频下载工具 - GUI 入口
"""
import os
import sys
import io

# 修复 Windows GBK 控制台编码问题
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel,
    QFileDialog, QInputDialog, QMessageBox, QFrame,
    QComboBox, QTabWidget, QPlainTextEdit,
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QPixmap

from core.extractor_factory import ExtractorFactory
from core.environment_checker import EnvironmentChecker
from core.download_manager import DownloadManager
from core.cookie_manager import BilibiliCookieManager, DouyinCookieManager
from core.database import DownloadHistoryDB
from core.task_queue import TaskQueue, DownloadTask
from gui.task_worker import TaskWorker
from gui.task_list_widget import TaskListWidget
from gui.history_widget import HistoryWidget

# ---- QSS 样式 ----
APP_STYLE = """
QMainWindow { background-color: #f5f5f5; }
QMenuBar { background-color: #fff; border-bottom: 1px solid #e0e0e0; padding: 2px; }
QMenuBar::item:selected { background-color: #fb7299; color: white; border-radius: 4px; }
QMenu { background-color: #fff; border: 1px solid #e0e0e0; }
QMenu::item:selected { background-color: #fb7299; color: white; }
QLineEdit {
    border: 1px solid #ddd; border-radius: 6px; padding: 8px 12px;
    font-size: 14px; background-color: #fff;
}
QLineEdit:focus { border-color: #fb7299; }
QPushButton {
    background-color: #fb7299; color: white; border: none; border-radius: 6px;
    padding: 8px 20px; font-size: 14px; font-weight: bold;
}
QPushButton:hover { background-color: #e0608a; }
QPushButton:pressed { background-color: #cc5078; }
QPushButton:disabled { background-color: #ccc; color: #888; }
QStatusBar { background-color: #fff; border-top: 1px solid #e0e0e0; color: #666; font-size: 12px; min-height: 24px; }
QComboBox {
    border: 1px solid #ddd; border-radius: 6px; padding: 6px 12px;
    font-size: 13px; background-color: #fff;
}
QComboBox:focus { border-color: #fb7299; }
QComboBox::drop-down { border: none; width: 24px; }
QFrame#info_card {
    background-color: #fff; border-radius: 10px;
    border: 1px solid #e8e8e8; padding: 12px;
}
"""


def _format_duration(seconds: int) -> str:
    """将秒数格式化为 mm:ss 或 hh:mm:ss"""
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_count(n: int) -> str:
    """将数字格式化为易读形式（如 12.3万）"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


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

        # 批量下载队列与并发工作线程
        self._task_queue = TaskQueue()
        self._active_workers: list[TaskWorker] = []

        self.setWindowTitle("视频下载工具")
        self.resize(900, 700)
        self._init_menu()
        self._init_ui()

        # 状态栏使用永久 QLabel，避免被临时消息覆盖
        self._status_bar_label = QLabel()
        self._status_bar_label.setStyleSheet("color: #666; font-size: 12px; padding: 0 8px;")
        self.statusBar().addPermanentWidget(self._status_bar_label, 1)
        self._update_status_bar()

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

        # 动态为每个需要 Cookie 的插件生成菜单项
        platforms = self._factory.get_registered_platforms()
        for platform in platforms:
            if not platform["requires_cookie"]:
                continue

            platform_id = platform["platform_id"]
            platform_name = platform["platform_name"]

            # 检查是否有对应的 CookieManager（支持扫码登录）
            has_login = self._has_cookie_manager(platform_id)

            if has_login:
                # 有扫码登录：创建子菜单
                cookie_menu = settings_menu.addMenu(f"{platform_name} Cookie")
                manual_action = cookie_menu.addAction("手动输入 Cookie")
                manual_action.triggered.connect(
                    lambda checked, pid=platform_id, pname=platform_name:
                        self._on_set_cookie_generic(pid, pname)
                )
                login_action = cookie_menu.addAction("扫码登录获取 Cookie")
                login_action.triggered.connect(
                    lambda checked, pid=platform_id, pname=platform_name:
                        self._on_login_cookie_generic(pid, pname)
                )
            else:
                # 无扫码登录：直接添加菜单项
                action = settings_menu.addAction(f"{platform_name} Cookie")
                action.triggered.connect(
                    lambda checked, pid=platform_id, pname=platform_name:
                        self._on_set_cookie_generic(pid, pname)
                )

    def _has_cookie_manager(self, platform_id: str) -> bool:
        """检查平台是否有对应的 CookieManager（支持扫码登录）"""
        return platform_id in ("bilibili", "douyin")

    def _init_ui(self):
        """初始化界面布局（Tab 页签结构）"""
        # 顶层 Tab 容器
        self._tab_widget = QTabWidget()
        self._tab_widget.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                padding: 8px 24px; font-size: 14px; border: none;
                background-color: transparent; color: #666;
            }
            QTabBar::tab:selected {
                color: #fb7299; border-bottom: 2px solid #fb7299; font-weight: bold;
            }
        """)
        self.setCentralWidget(self._tab_widget)

        # ---- Tab 1: 下载 ----
        download_tab = QWidget()
        layout = QVBoxLayout(download_tab)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # URL 输入区域
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self._url_input = QPlainTextEdit()
        self._url_input.setPlaceholderText("请输入视频链接 / BV号 / AV号（支持多行批量输入）")
        self._url_input.setFixedHeight(80)
        self._url_input.setStyleSheet(
            "QPlainTextEdit { border: 1px solid #ddd; border-radius: 6px; "
            "padding: 8px 12px; font-size: 14px; background-color: #fff; }"
            "QPlainTextEdit:focus { border-color: #fb7299; }")
        self._parse_btn = QPushButton("解析")
        self._parse_btn.setFixedWidth(80)
        self._parse_btn.setFixedHeight(80)
        self._parse_btn.clicked.connect(self._on_parse)
        input_row.addWidget(self._url_input)
        input_row.addWidget(self._parse_btn)
        layout.addLayout(input_row)

        # 状态提示
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-size: 13px;")
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
        self._cover_label.setStyleSheet("background-color: #eee; border-radius: 6px;")
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setScaledContents(True)
        info_layout.addWidget(self._cover_label)

        # 文字信息区
        text_layout = QVBoxLayout()
        text_layout.setSpacing(6)

        self._title_label = QLabel("")
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #333;")
        text_layout.addWidget(self._title_label)

        self._owner_label = QLabel("")
        self._owner_label.setStyleSheet("font-size: 13px; color: #fb7299;")
        text_layout.addWidget(self._owner_label)

        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("font-size: 13px; color: #999;")
        text_layout.addWidget(self._meta_label)

        text_layout.addStretch()
        info_layout.addLayout(text_layout, 1)
        layout.addWidget(self._info_card)

        # 清晰度选择行
        quality_row = QHBoxLayout()
        quality_row.setSpacing(8)
        self._quality_label = QLabel("清晰度：")
        self._quality_label.setStyleSheet("font-size: 13px; color: #666;")
        self._quality_label.setVisible(False)
        self._quality_combo = QComboBox()
        self._quality_combo.setMinimumWidth(280)
        self._quality_combo.setVisible(False)
        quality_row.addWidget(self._quality_label)
        quality_row.addWidget(self._quality_combo)
        quality_row.addStretch()
        layout.addLayout(quality_row)

        # 添加到队列按钮
        self._download_btn = QPushButton("添加到下载队列")
        self._download_btn.setEnabled(False)
        self._download_btn.setFixedHeight(40)
        self._download_btn.clicked.connect(self._on_add_to_queue)
        layout.addWidget(self._download_btn)

        # 任务队列列表
        self._task_list_widget = TaskListWidget(self._task_queue, parent=self)
        layout.addWidget(self._task_list_widget, 1)

        self._tab_widget.addTab(download_tab, "下载")

        # ---- Tab 2: 历史记录 ----
        self._history_widget = HistoryWidget(self._db, factory=self._factory, parent=self)
        self._tab_widget.addTab(self._history_widget, "历史记录")

        # 切换到历史记录 Tab 时自动刷新
        self._tab_widget.currentChanged.connect(self._on_tab_changed)

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

        # 动态检查所有平台的 Cookie 状态
        platforms = self._factory.get_registered_platforms()
        for platform in platforms:
            if not platform["requires_cookie"]:
                continue
            platform_id = platform["platform_id"]
            platform_name = platform["platform_name"]
            cookie = self._env_checker.get_cookie_for_platform(platform_id)
            status = "已配置" if cookie else "未配置"
            parts.append(f"{platform_name} Cookie {status}")

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

    # ---- Cookie 设置（通用方法）----

    def _on_set_cookie_generic(self, platform_id: str, platform_name: str):
        """通用的手动输入 Cookie 方法"""
        current_cookie = self._env_checker.get_cookie_for_platform(platform_id)
        cookie, ok = QInputDialog.getText(
            self, f"设置 {platform_name} Cookie",
            f"请粘贴 {platform_name} Cookie：", text=current_cookie)
        if ok:
            self._env_checker.set_cookie_for_platform(platform_id, cookie)
            self._status_label.setText(f"{platform_name} Cookie 已保存" if cookie.strip() else f"{platform_name} Cookie 已清除")
            self._update_status_bar()

    def _on_login_cookie_generic(self, platform_id: str, platform_name: str):
        """通用的扫码登录获取 Cookie 方法"""
        # 根据 platform_id 选择对应的 CookieManager
        if platform_id == "bilibili":
            manager_class = BilibiliCookieManager
        elif platform_id == "douyin":
            manager_class = DouyinCookieManager
        else:
            QMessageBox.warning(self, "不支持", f"{platform_name} 暂不支持扫码登录")
            return

        reply = QMessageBox.question(
            self, "扫码登录",
            f"即将打开浏览器，请在浏览器中完成 {platform_name} 登录。\n"
            f"登录成功后会自动获取 Cookie 并关闭浏览器。\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        class LoginWorker(QThread):
            success = Signal(str)
            failed = Signal(str)

            def __init__(self, manager_cls):
                super().__init__()
                self.manager_cls = manager_cls

            def run(self):
                try:
                    manager = self.manager_cls()
                    cookie = manager.get_cookie_by_login()
                    if cookie:
                        self.success.emit(cookie)
                    else:
                        self.failed.emit("超时或用户取消")
                except Exception as e:
                    self.failed.emit(f"浏览器启动失败: {str(e)}")

        def on_success(cookie):
            self._env_checker.set_cookie_for_platform(platform_id, cookie)
            self._status_label.setText(f"{platform_name} Cookie 获取成功并已保存")
            self._update_status_bar()
            QMessageBox.information(self, "成功", f"{platform_name} Cookie 已保存到配置文件")

        def on_failed(error_msg):
            self._status_label.setText(f"{platform_name} Cookie 获取失败")
            QMessageBox.warning(self, "失败", f"未能获取 {platform_name} Cookie\n错误: {error_msg}")

        self._status_label.setText(f"正在打开浏览器，请在浏览器中登录 {platform_name}...")
        worker = LoginWorker(manager_class)
        worker.success.connect(on_success)
        worker.failed.connect(on_failed)
        worker.start()
        self._login_worker = worker  # 保持引用避免被 GC

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
        self._parse_btn.setEnabled(False)

        extractor = self._factory.get_extractor(url)
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

        worker = ParseWorker(extractor, url, cookie)
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
                        extractor = self._factory.get_extractor(url)
                        if extractor is None:
                            self.one_error.emit(url, "不支持的链接")
                            continue
                        # 根据插件的 PLATFORM_ID 自动获取对应的 Cookie
                        cookie = ""
                        if extractor.REQUIRES_COOKIE:
                            cookie = self._env.get_cookie_for_platform(extractor.PLATFORM_ID)
                        info = extractor.extract_info(url, cookie=cookie)
                        self.one_done.emit(url, info)
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

        # 加载封面图
        cover_url = info.get("cover_url", "")
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=5)
                pixmap = QPixmap()
                pixmap.loadFromData(resp.content)
                self._cover_label.setPixmap(pixmap)
            except Exception:
                self._cover_label.setText("封面加载失败")

        self._info_card.setVisible(True)
        self._download_btn.setEnabled(True)

        # 填充清晰度下拉框
        self._quality_combo.clear()
        streams = info.get("streams", [])
        video_streams = [s for s in streams if s.get("type") == "video"]
        if video_streams:
            seen = set()
            for s in video_streams:
                qid = s["quality_id"]
                if qid in seen:
                    continue
                seen.add(qid)
                label = f"{s['quality_name']} ({s.get('codecs', '')}) | {s.get('bandwidth', 0) // 1000}kbps"
                self._quality_combo.addItem(label, qid)
            self._quality_combo.setVisible(True)
            self._quality_label.setVisible(True)
        else:
            self._quality_combo.setVisible(False)
            self._quality_label.setVisible(False)

        platform = info.get("platform", "")
        # 动态检查当前平台的 Cookie 状态
        cookie = self._env_checker.get_cookie_for_platform(platform)
        has_cookie = bool(cookie)

        # 根据平台查找对应的插件元数据
        platforms = self._factory.get_registered_platforms()
        platform_info = next((p for p in platforms if p["platform_id"] == platform), None)

        if platform_info and platform_info["requires_cookie"] and not has_cookie:
            platform_name = platform_info["platform_name"]
            self._status_label.setText(f"解析成功（未配置 {platform_name} Cookie，可能无法获取高清或无水印视频）")
        else:
            self._status_label.setText("解析成功")

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

        quality_id = self._quality_combo.currentData() if self._quality_combo.isVisible() else None
        url = self._url_input.toPlainText().strip()

        task = DownloadTask(
            url=url,
            video_info=self._current_video_info,
            quality_id=quality_id,
            save_dir=save_dir,
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
            worker.finished.connect(lambda _=None, w=worker: self._on_worker_done(w))
            self._active_workers.append(worker)
            self._task_list_widget.refresh()
            worker.start()

    def _on_task_progress(self, task_id: str, downloaded: int, total: int):
        """更新指定任务的进度"""
        self._task_list_widget.update_progress(task_id, downloaded, total)

    def _on_task_finished(self, task_id: str, file_path: str):
        """任务下载完成，写入数据库记录"""
        task = self._task_queue.get_task(task_id)
        self._task_list_widget.mark_completed(task_id)

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

    def _on_worker_done(self, worker: TaskWorker):
        """工作线程结束后清理并尝试启动下一个任务"""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        self._try_start_next()

    def closeEvent(self, event):
        """窗口关闭时清理所有插件的常驻资源"""
        # 遍历所有已注册的插件类，调用 close_browser（如果有的话）
        seen = set()
        for cls in self._factory._registry.values():
            if cls not in seen:
                seen.add(cls)
                if hasattr(cls, "close_browser"):
                    try:
                        cls.close_browser()
                    except Exception:
                        pass
        super().closeEvent(event)


def main():
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
