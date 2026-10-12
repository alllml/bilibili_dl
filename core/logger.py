"""
日志系统

【为什么要】打包后是 --noconsole（无控制台），一旦出问题用户拿不到任何
现场信息，无法反馈、无法排查。这里把日志落盘，并把 print 输出一并接管。

日志位置：%LOCALAPPDATA%/VideoDownloader/logs/app.log（滚动保留 3 份）

【重要】--noconsole 下 sys.stdout / sys.stderr 都是 None：
  * 不能直接给 root 挂 StreamHandler —— 否则 emit 里 None.write 抛异常，
    经 logging.handleError 回滚写 sys.stderr 又会触发同样的 handler，
    形成无限递归，最终 RecursionError 导致程序启动即崩（静默闪退）。
  * 因此：只在有真实终端时才挂 StreamHandler；print 接管后直接写文件，
    不再经过 logging 管道，杜绝递归。
"""
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler


def get_log_dir() -> str:
    """返回日志目录（不存在则创建）"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "VideoDownloader", "logs")
    os.makedirs(path, exist_ok=True)
    return path


def get_log_file() -> str:
    return os.path.join(get_log_dir(), "app.log")


# 文件 handler 的模块级引用，供 _TeeStream 绕过 logging 管道直接写文件
_file_handler: RotatingFileHandler | None = None


def _write_to_file(levelno: int, msg: str) -> None:
    """把一条消息直接写进日志文件（绕过 logging 管道，避免 handler 递归）。"""
    if _file_handler is None:
        return
    try:
        record = logging.LogRecord(
            "tee", levelno, pathname="", lineno=0, msg=msg, args=None, exc_info=None)
        _file_handler.emit(record)
    except Exception:
        pass


class _TeeStream:
    """
    把 print 的输出同时写到「原始终端」和「日志文件」。
    打包后原始终端可能为 None，此时只写日志。
    """

    def __init__(self, original, levelno):
        self._original = original
        self._levelno = levelno

    def write(self, msg):
        if self._original is not None:
            try:
                self._original.write(msg)
            except Exception:
                pass
        text = (msg or "").rstrip()
        if text:
            _write_to_file(self._levelno, text)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    @property
    def buffer(self):
        """兼容外部代码访问 sys.stdout.buffer 的情况（原流无 buffer 时返回 None）"""
        return getattr(self._original, "buffer", None)


def setup_logging(level=logging.INFO) -> str:
    """
    初始化日志：控制台（如有）+ 滚动文件；并把 sys.stdout / sys.stderr 接到日志。
    返回日志文件路径。
    """
    global _file_handler

    root = logging.getLogger()
    root.setLevel(level)

    # 避免重复添加 handler（多次调用时）
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

        _file_handler = RotatingFileHandler(
            get_log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _file_handler.setFormatter(formatter)
        root.addHandler(_file_handler)

        # 只有存在真实终端才挂控制台 handler。
        # 无控制台（打包版）下 sys.stderr 为 None，挂上去会引发
        # None.write -> handleError -> 写 sys.stderr -> 递归崩溃。
        if sys.stderr is not None:
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            root.addHandler(console)

    # 接管 print（打包后 sys.stdout 可能为 None）；直接写文件，不经过 logging 管道
    sys.stdout = _TeeStream(getattr(sys, "stdout", None), logging.INFO)
    sys.stderr = _TeeStream(getattr(sys, "stderr", None), logging.ERROR)

    logging.getLogger(__name__).info("日志系统已启动 -> %s", get_log_file())
    return get_log_file()
