"""`jtdt update` 的健康檢查要探對地方，失敗要說得出原因。

由來（v1.15.15，客戶回報）：更新跑完印出 `Health check timed out`。
`jtdt bind` 把監聽位址寫進**服務管理員自己的設定**（systemd 的
`Environment=`、macOS 的 launcher、Windows 的 WinSW XML），而健康檢查是從
**管理員 shell 的環境變數**讀的 —— `sudo jtdt update` 根本繼承不到。於是
只要有人改過 port 或綁到某一個網卡位址，健康檢查就永遠探 127.0.0.1:8765、
永遠逾時，**而服務其實是好的**。
"""
from __future__ import annotations

import importlib

import pytest

cli = importlib.import_module("app.cli")

_UNIT = """[Service]
Environment=JTDT_DATA_DIR=/var/lib/jt-doc-tools/data
Environment=JTDT_HOST=0.0.0.0
Environment=JTDT_PORT=9443
ExecStart=/opt/jt-doc-tools/.venv/bin/python -m app.main
"""
_LAUNCHER = 'exec env JTDT_HOST=127.0.0.1 JTDT_PORT=9999 JTDT_DATA_DIR=/x python\n'
_WINSW = ('<service><env name="JTDT_HOST" value="192.168.1.5"/>'
          '<env name="JTDT_PORT" value="8080"/></service>')


@pytest.fixture
def _no_shell_env(monkeypatch):
    """模擬 `sudo jtdt update`：shell 裡沒有那兩個變數。"""
    monkeypatch.delenv("JTDT_HOST", raising=False)
    monkeypatch.delenv("JTDT_PORT", raising=False)


def _fake_platform(monkeypatch, *, linux=False, macos=False, windows=False):
    monkeypatch.setattr(cli, "_is_linux", lambda: linux)
    monkeypatch.setattr(cli, "_is_macos", lambda: macos)
    monkeypatch.setattr(cli, "_is_windows", lambda: windows)


@pytest.mark.parametrize("text,want", [
    (_UNIT, ("0.0.0.0", "9443")),
    (_LAUNCHER, ("127.0.0.1", "9999")),
    (_WINSW, ("192.168.1.5", "8080")),
])
def test_the_parser_understands_all_three_service_formats(
        monkeypatch, text, want):
    """systemd / macOS launcher / WinSW XML 三種格式都要讀得出來。"""
    _fake_platform(monkeypatch, linux=True)
    monkeypatch.setattr(cli.Path, "exists", lambda self: True, raising=False)
    monkeypatch.setattr(cli.Path, "read_text",
                        lambda self, **kw: text, raising=False)
    assert cli._service_bind() == want


def test_server_url_uses_the_service_configuration(monkeypatch, _no_shell_env):
    """這是整件事的核心：shell 沒有那兩個變數時，要去問服務怎麼綁的。

    `jtdt status` 印出來的網址也是走這裡 —— 改過 port 的安裝原本會印錯。
    """
    monkeypatch.setattr(cli, "_service_bind", lambda: ("192.168.1.5", "9443"))
    assert cli._server_url() == "http://192.168.1.5:9443/"


def test_a_wildcard_bind_is_shown_as_an_address_you_can_open(monkeypatch,
                                                             _no_shell_env):
    """`jtdt status` / `jtdt open` 印 `http://0.0.0.0:8765/` 是沒有用的。"""
    monkeypatch.setattr(cli, "_service_bind", lambda: ("0.0.0.0", "9443"))
    assert cli._server_url() == "http://127.0.0.1:9443/"


def test_shell_environment_still_wins(monkeypatch):
    """前景執行（`jtdt run`）是靠環境變數驅動的，不可以被服務設定蓋掉。"""
    monkeypatch.setattr(cli, "_service_bind", lambda: ("0.0.0.0", "9443"))
    monkeypatch.setenv("JTDT_HOST", "127.0.0.1")
    monkeypatch.setenv("JTDT_PORT", "8765")
    assert cli._server_url() == "http://127.0.0.1:8765/"


def test_a_wildcard_bind_is_probed_on_loopback(monkeypatch, _no_shell_env):
    """`0.0.0.0` 是**綁定**位址，不是連得上的位址。"""
    monkeypatch.setattr(cli, "_service_bind", lambda: ("0.0.0.0", "8765"))
    assert cli._health_urls() == ["http://127.0.0.1:8765/healthz"]


def test_a_lan_bind_also_falls_back_to_loopback(monkeypatch, _no_shell_env):
    """綁在某一張網卡時，loopback 也要探 —— 猜錯位址不可以自己害檢查失敗。"""
    monkeypatch.setattr(cli, "_service_bind", lambda: ("192.168.1.5", "8080"))
    assert cli._health_urls() == ["http://192.168.1.5:8080/healthz",
                                  "http://127.0.0.1:8080/healthz"]


def test_no_service_config_falls_back_to_the_defaults(monkeypatch, _no_shell_env):
    monkeypatch.setattr(cli, "_service_bind", lambda: (None, None))
    assert cli._server_url() == "http://127.0.0.1:8765/"


def test_the_local_probe_does_not_go_through_a_proxy():
    """企業環境的管理員 shell 常設著 `http_proxy`。

    探測自己這台機器時走代理是錯的 —— 連不上會被讀成「服務沒起來」。
    """
    from app.core import safe_fetch
    import inspect
    src = inspect.getsource(safe_fetch.urlopen_direct)
    assert "ProxyHandler({})" in src
    assert "urlopen_direct" in inspect.getsource(cli.svc_update)
