"""
日志系统

【为什么要】打包后是 --noconsole（无控制台），一旦出问题用户拿不到任何
现场信息，无法反馈、无法排查。这里把日志落盘，并把 print 输出一并接管。

日志位置：%LOCALAPPDATA%/VideoDownloader/logs/app.log（滚动保留 3 份）
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


class _TeeStream:
    """
    把 print 的输出同时写到「原始终端」和「日志文件」。
    打包后原始终端可能为 None，此时只写日志。
    """

    def __init__(self, original, logger_obj, level):
        self._original = original
        self._logger = logger_obj
        self._level = level

    def write(self, msg):
        if self._original is not None:
            try:
                self._original.write(msg)
            except Exception:
                pass
        text = (msg or "").rstrip()
        if text:
            self._logger.log(self._level, text)

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
    初始化日志：控制台 + 滚动文件；并把 sys.stdout / sys.stderr 接到日志。
    返回日志文件路径。
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 避免重复添加 handler（多次调用时）
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

        file_handler = RotatingFileHandler(
            get_log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    # 接管 print（打包后 sys.stdout 可能为 None）
    sys.stdout = _TeeStream(getattr(sys, "stdout", None), logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _TeeStream(getattr(sys, "stderr", None), logging.getLogger("stderr"), logging.ERROR)

    logging.getLogger(__name__).info("日志系统已启动 -> %s", get_log_file())
    return get_log_file()
