"""
Cookie 设置对话框（面向完全不懂技术的用户）

设计目标：
1. 首选「一键扫码登录」——点一下、扫个码，Cookie 自动获取并回填；
2. 也支持「整段粘贴」——抖音 Cookie 分段很多，用户无需手动拆，
   程序会自动挑出 sessionid / ttwid 等关键字段；
3. 全程有文字提示与实时校验（已识别哪些字段、还缺什么）。
"""
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QFrame, QMessageBox,
)

from core.cookie_utils import (
    analyze, check_cookie_status, verify_cookie_online, format_expire_time,
)

# 各类状态对应的文字颜色
_STATE_COLORS = {
    "missing": "#8A9099",
    "expired": "#D9363E",
    "expiring": "#D46B08",
    "valid": "#1F9254",
    "unknown": "#8A9099",
}


# 各平台的通俗说明与图文步骤
_PLATFORM_INFO: dict[str, dict[str, str]] = {
    "bilibili": {
        "why": "不设置的话，只能下载 480P 及以下的清晰度。",
        "key_hint": "关键字段：SESSDATA",
        "steps": (
            "1. 用 Edge 或 Chrome 打开 bilibili.com 并登录（扫码即可）；\n"
            "2. 按 F12 打开开发者工具，顶部选择 Application（应用）；\n"
            "3. 左侧展开 Cookies，点选 https://www.bilibili.com；\n"
            "4. 找到 SESSDATA 这一行，复制它的值；\n"
            "5. 粘贴到上面的输入框（只贴 SESSDATA 的值也可以，会自动识别）。"
        ),
    },
    "douyin": {
        "why": "不设置的话，可能只能下载到带水印或低清晰度的视频。",
        "key_hint": "关键字段：sessionid、ttwid（其余会自动精简掉）",
        "steps": (
            "最快的方式：直接点上面的「一键扫码登录」，无需手动操作。\n\n"
            "若想手动获取：\n"
            "1. 用 Edge 或 Chrome 打开 douyin.com 并登录；\n"
            "2. 按 F12 → Application（应用）→ Cookies → https://www.douyin.com；\n"
            "3. 不用挑，全选复制即可（抖音 Cookie 分段很多，这是正常的）；\n"
            "4. 整段粘贴到上面的输入框 —— 程序会自动挑出需要的字段。"
        ),
    },
    "twitter": {
        "why": "不设置的话，部分推文可能无法解析。",
        "key_hint": "关键字段：auth_token、ct0",
        "steps": (
            "1. 用 Edge 或 Chrome 打开 x.com 并登录；\n"
            "2. 按 F12 → Application（应用）→ Cookies → https://x.com；\n"
            "3. 整段复制 Cookie 后粘贴到上面的输入框。"
        ),
    },
}

_DEFAULT_INFO = {
    "why": "配置后可使用需要登录才能访问的内容。",
    "key_hint": "整段粘贴即可。",
    "steps": "打开网站登录后，复制浏览器中的 Cookie 并粘贴到上面的输入框。",
}


class _LoginWorker(QThread):
    """在后台线程中执行 Selenium 扫码登录，避免阻塞界面，并实时汇报进度。"""

    success = Signal(str)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, manager_cls, parent=None):
        super().__init__(parent)
        self._manager_cls = manager_cls
        self._manager = None

    def run(self):
        try:
            self._manager = self._manager_cls()
            cookie = self._manager.get_cookie_by_login(status_callback=self.status.emit)
            if cookie:
                self.success.emit(cookie)
            else:
                self.failed.emit("等待超时或已取消")
        except Exception as e:
            self.failed.emit(str(e))

    def stop(self):
        """请求取消登录等待（由界面线程调用）"""
        if self._manager is not None:
            self._manager.cancel()


class _VerifyWorker(QThread):
    """后台在线校验 Cookie 是否真的还能用（避免阻塞界面）"""

    done = Signal(object, str)  # (True/False/None, 说明文本)

    def __init__(self, platform_id: str, cookie: str, parent=None):
        super().__init__(parent)
        self._platform_id = platform_id
        self._cookie = cookie

    def run(self):
        ok, msg = verify_cookie_online(self._platform_id, self._cookie)
        self.done.emit(ok, msg)


class CookieDialog(QDialog):
    """Cookie 设置对话框：扫码登录 + 整段粘贴自动识别。"""

    def __init__(self, platform_id: str, platform_name: str,
                 current_cookie: str = "", login_manager_cls=None, parent=None):
        super().__init__(parent)
        self._platform_id = platform_id
        self._platform_name = platform_name
        self._login_manager_cls = login_manager_cls
        self._login_worker = None
        self._verify_worker = None

        self.setWindowTitle(f"设置 {platform_name} Cookie")
        self.setMinimumWidth(540)
        self._init_ui(current_cookie)

    # ---- UI 构建 ----

    def _init_ui(self, current_cookie: str):
        info = _PLATFORM_INFO.get(self._platform_id, _DEFAULT_INFO)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        # 1) 为什么需要
        why = QLabel(f"为什么要设置：{info['why']}")
        why.setWordWrap(True)
        why.setStyleSheet("color: #646A73; font-size: 13px; background: transparent;")
        layout.addWidget(why)

        # 1.5) 当前已保存 Cookie 的状态：直接回答「我这个 Cookie 还能用吗」
        if current_cookie:
            layout.addWidget(self._build_current_status_card(current_cookie))

        # 2) 一键扫码登录（有对应管理器时才显示）
        if self._login_manager_cls is not None:
            layout.addWidget(self._build_login_card())

        # 3) 手动粘贴区
        paste_title = QLabel("或者：直接粘贴 Cookie（整段贴进来即可，无需手动挑字段）")
        paste_title.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #1F2329; background: transparent;")
        layout.addWidget(paste_title)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(
            "在此粘贴 Cookie，例如：\n"
            "sessionid=xxxx; ttwid=xxxx; sid_guard=xxxx ...\n"
            "（支持整段 Cookie、请求头 Cookie: xxx、浏览器插件导出的 JSON）")
        self._editor.setFixedHeight(96)
        self._editor.textChanged.connect(self._refresh_hint)
        layout.addWidget(self._editor)

        # 4) 实时校验提示
        self._hint_label = QLabel("")
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("font-size: 12px; background: transparent;")
        layout.addWidget(self._hint_label)

        # 5) 获取步骤（默认折叠为一行提示，点「查看获取步骤」展开）
        self._steps_frame = self._build_steps_card(info)
        layout.addWidget(self._steps_frame)

        # 6) 底部按钮
        layout.addLayout(self._build_buttons())

        # 初始化
        if current_cookie:
            self._editor.setPlainText(current_cookie)
        self._refresh_hint()

    def _build_login_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background-color: #FDEFF4; border: 1px solid #FBCFE0;"
            " border-radius: 10px; }")
        box = QHBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(4)
        title = QLabel("推荐：一键扫码登录")
        title.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #D44A74; background: transparent;")
        desc = QLabel("点击按钮后会打开浏览器，登录成功后自动获取 Cookie 并填入下方。")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px; color: #993556; background: transparent;")
        text.addWidget(title)
        text.addWidget(desc)
        box.addLayout(text, 1)

        self._login_btn = QPushButton("打开浏览器登录")
        self._login_btn.setFixedHeight(34)
        self._login_btn.setStyleSheet(
            "QPushButton { padding: 6px 16px; font-size: 13px; }"
            "QPushButton:disabled { background-color: #F0C7D6; color: #FFFFFF; }")
        self._login_btn.clicked.connect(self._on_login)
        box.addWidget(self._login_btn)

        self._login_status = QLabel("")
        self._login_status.setStyleSheet("font-size: 12px; color: #993556; background: transparent;")
        # 状态放在卡片下方单独一行
        wrapper = QFrame()
        wrapper.setStyleSheet("QFrame { background: transparent; border: none; }")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(6)
        wl.addWidget(card)
        wl.addWidget(self._login_status)
        self._login_wrapper = wrapper
        return wrapper

    @staticmethod
    def _status_text(state: dict) -> str:
        """把状态字典转成给用户看的文字"""
        expire = format_expire_time(state.get("expire_at"))
        if state["state"] == "expired":
            return f"已过期（{expire} 到期）"
        if state["state"] == "expiring":
            return f"即将过期（{expire} 到期）"
        if state["state"] == "valid":
            return f"有效（{expire} 到期）"
        if state["state"] == "missing":
            return "未配置"
        return "已配置（无法判断有效期）"

    def _build_current_status_card(self, current_cookie: str) -> QFrame:
        """展示已保存 Cookie 的有效期，并提供在线校验按钮"""
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background-color: #FFFFFF; border: 1px solid #EDEFF3;"
            " border-radius: 10px; }")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(6)

        state = check_cookie_status(self._platform_id, current_cookie)
        color = _STATE_COLORS.get(state["state"], "#646A73")

        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(f"当前已保存的 Cookie：{self._status_text(state)}")
        label.setWordWrap(True)
        label.setStyleSheet(f"font-size: 13px; color: {color}; background: transparent;")
        row.addWidget(label, 1)
        self._current_status_label = label

        self._verify_btn = QPushButton("验证有效性")
        self._verify_btn.setFixedHeight(28)
        self._verify_btn.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #646A73;"
            " border: 1px solid #E1E4EA; border-radius: 6px;"
            " padding: 2px 12px; font-size: 12px; font-weight: 500; }"
            "QPushButton:hover { background-color: #F5F6F8; }")
        self._verify_btn.clicked.connect(lambda: self._on_verify(current_cookie))
        row.addWidget(self._verify_btn)
        box.addLayout(row)

        self._verify_result = QLabel("")
        self._verify_result.setWordWrap(True)
        self._verify_result.setStyleSheet(
            "font-size: 12px; color: #8A9099; background: transparent;")
        box.addWidget(self._verify_result)
        return frame

    def _on_verify(self, cookie: str):
        """在线校验当前保存的 Cookie（后台线程）"""
        if self._verify_worker is not None and self._verify_worker.isRunning():
            return
        self._verify_btn.setEnabled(False)
        self._verify_result.setText("正在联网校验…")
        self._verify_result.setStyleSheet(
            "font-size: 12px; color: #8A9099; background: transparent;")

        self._verify_worker = _VerifyWorker(self._platform_id, cookie)
        self._verify_worker.done.connect(self._on_verify_done)
        self._verify_worker.start()

    def _on_verify_done(self, ok, msg: str):
        self._verify_btn.setEnabled(True)
        if ok is True:
            color, prefix = "#1F9254", "✓"
        elif ok is False:
            color, prefix = "#D9363E", "✗"
        else:
            color, prefix = "#8A9099", "ℹ"
        self._verify_result.setText(f"{prefix} {msg}")
        self._verify_result.setStyleSheet(
            f"font-size: 12px; color: {color}; background: transparent;")

    def _build_steps_card(self, info: dict) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background-color: #FFFFFF; border: 1px solid #EDEFF3;"
            " border-radius: 10px; }")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 10, 14, 12)
        box.setSpacing(8)

        self._steps_btn = QPushButton("查看手动获取步骤 ▾")
        self._steps_btn.setCheckable(True)
        self._steps_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #646A73; border: none;"
            " text-align: left; padding: 0; font-size: 13px; }"
            "QPushButton:hover { color: #D44A74; }")
        self._steps_btn.toggled.connect(self._on_toggle_steps)
        box.addWidget(self._steps_btn)

        self._steps_label = QLabel(info["steps"])
        self._steps_label.setWordWrap(True)
        self._steps_label.setStyleSheet(
            "font-size: 12px; color: #8A9099; background: transparent; line-height: 170%;")
        self._steps_label.setVisible(False)
        box.addWidget(self._steps_label)

        key_hint = QLabel(info["key_hint"])
        key_hint.setWordWrap(True)
        key_hint.setVisible(False)
        key_hint.setStyleSheet("font-size: 12px; color: #FB7299; background: transparent;")
        box.addWidget(key_hint)
        self._key_hint_label = key_hint

        return frame

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._clear_btn = QPushButton("清除")
        self._clear_btn.setFixedHeight(34)
        self._clear_btn.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #646A73;"
            " border: 1px solid #E1E4EA; border-radius: 8px; padding: 6px 16px; }"
            "QPushButton:hover { background-color: #F5F6F8; }")
        self._clear_btn.clicked.connect(lambda: self._editor.setPlainText(""))
        row.addWidget(self._clear_btn)

        row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setStyleSheet(
            "QPushButton { background-color: #FFFFFF; color: #646A73;"
            " border: 1px solid #E1E4EA; border-radius: 8px; padding: 6px 18px; }"
            "QPushButton:hover { background-color: #F5F6F8; }")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)

        self._save_btn = QPushButton("保存")
        self._save_btn.setFixedHeight(34)
        self._save_btn.setStyleSheet(
            "QPushButton { padding: 6px 22px; font-size: 13px; }")
        self._save_btn.clicked.connect(self._on_save)
        row.addWidget(self._save_btn)

        return row

    # ---- 交互逻辑 ----

    def _on_toggle_steps(self, checked: bool):
        self._steps_label.setVisible(checked)
        self._key_hint_label.setVisible(checked)
        self._steps_btn.setText(
            "收起手动获取步骤 ▴" if checked else "查看手动获取步骤 ▾")

    def _refresh_hint(self):
        """实时显示「已识别 / 还缺什么」"""
        text = self._editor.toPlainText()
        if not text.strip():
            self._hint_label.setText("")
            self._hint_label.setStyleSheet("font-size: 12px; background: transparent;")
            return

        _cookie, present, missing, recognized = analyze(text, self._platform_id)
        if not recognized:
            self._hint_label.setText(
                "⚠ 没识别到关键字段，请确认粘贴的是完整的 Cookie（可展开下方步骤查看）。")
            self._hint_label.setStyleSheet(
                "font-size: 12px; color: #D9363E; background: transparent;")
            return

        parts = [f"✓ 已识别 {len(present)} 个关键字段：{'、'.join(present) or '无'}"]
        if missing:
            parts.append(f"⚠ 缺少必需字段：{'、'.join(missing)}，按此保存可能无效")
            color = "#D9363E"
        else:
            parts.append("关键字段齐全，可以保存")
            color = "#1F9254"
        self._hint_label.setText("　".join(parts))
        self._hint_label.setStyleSheet(f"font-size: 12px; color: {color}; background: transparent;")

    def _on_login(self):
        if self._login_worker is not None and self._login_worker.isRunning():
            return
        self._login_btn.setEnabled(False)
        self._login_status.setText("正在启动浏览器…")
        self._login_status.setStyleSheet(
            "font-size: 12px; color: #993556; background: transparent;")

        self._login_worker = _LoginWorker(self._login_manager_cls)
        self._login_worker.success.connect(self._on_login_success)
        self._login_worker.failed.connect(self._on_login_failed)
        self._login_worker.status.connect(self._on_login_status)
        self._login_worker.start()

    def _on_login_success(self, cookie: str):
        self._editor.setPlainText(cookie)
        self._login_btn.setEnabled(True)
        self._login_status.setText("✓ 登录成功，Cookie 已自动填入，点「保存」即可。")
        self._login_status.setStyleSheet(
            "font-size: 12px; color: #1F9254; background: transparent;")

    def _on_login_status(self, msg: str):
        """实时显示登录进度（启动浏览器 / 等待扫码 / 已等待秒数 / 成功）"""
        self._login_status.setText(msg)
        self._login_status.setStyleSheet(
            "font-size: 12px; color: #993556; background: transparent;")

    def _on_login_failed(self, msg: str):
        self._login_btn.setEnabled(True)
        self._login_status.setText(f"✗ 登录未完成：{msg}")
        self._login_status.setStyleSheet(
            "font-size: 12px; color: #D9363E; background: transparent;")

    # ---- 关闭时取消等待并释放浏览器 ----

    def _stop_login(self):
        worker = self._login_worker
        if worker is not None and worker.isRunning():
            worker.stop()          # 通知管理器取消轮询
            worker.wait(3000)      # 等它关闭浏览器（轮询间隔 0.4s，通常很快）

        verify = self._verify_worker
        if verify is not None and verify.isRunning():
            verify.wait(3000)

    def reject(self):
        self._stop_login()
        super().reject()

    def closeEvent(self, event):
        self._stop_login()
        super().closeEvent(event)

    def _on_save(self):
        text = self._editor.toPlainText().strip()
        if text:
            _cookie, present, missing, recognized = analyze(text, self._platform_id)
            if missing:
                reply = QMessageBox.warning(
                    self, "关键字段缺失",
                    f"缺少必需字段：{'、'.join(missing)}，可能导致功能不可用。\n\n仍要保存吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    return
        self.accept()

    # ---- 对外接口 ----

    def get_cookie(self) -> str:
        """返回用户输入（原始字符串，由上层负责规整落盘）"""
        return self._editor.toPlainText().strip()
