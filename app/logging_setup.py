import logging
import sys


def _utf8_streams() -> None:
    """stdout / stderr 一律用 UTF-8 寫。

    Windows 服務由 WinSW 包著，stdout 是導進記錄檔的管線，Python 用系統的 ANSI
    字碼頁編碼。「非 Unicode 程式語系」是英文的機器（cp1252 —— 企業的英文版
    Windows Server 多半如此）上，**每一行含中文的記錄一寫就 UnicodeEncodeError**，
    logging 只在 stderr 留一段堆疊、那一行記錄本身就不見了（v1.16.56 在 Windows
    實機看到：一次啟動 100 行「Registered tool」全掉）。Linux 那邊 cli.py 早就替
    systemd 補了 PYTHONIOENCODING=utf-8；這裡從程式端修，所有平台、既有安裝更新後
    都生效，不必重新產生服務設定。

    寫不出來的字用反斜線跳脫，不丟例外 —— 記錄是附屬品，不可以讓它弄壞呼叫端。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("_", "-")
        if stream is None or enc in ("utf-8", "utf8"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def setup_logging(level: str = "INFO") -> None:
    _utf8_streams()
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
