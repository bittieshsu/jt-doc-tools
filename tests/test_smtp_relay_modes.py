"""通知信的三種寄送方式。

原本只有一種：填 SMTP 主機 + 帳號密碼。使用者要的兩種都是「**不用帳號密碼**，
對方的 mail server 依來源 IP 信任 jtdt」：

  * `relay`  —— 送到他們公司的 mail server，由它轉送出去（企業內部最常見）
  * `direct` —— jtdt 自己查收件網域的 MX 直接送，不經過任何中繼主機

判準的重點：**舊安裝的行為一個位元都不能變**（沒有 `smtp_mode` 這個欄位的
設定檔要照舊走 auth），以及 relay / direct 不可以偷偷去做認證。
"""
from __future__ import annotations

import pytest

import app.core.notify_channels as nc
import app.core.notify_settings as ns


class _FakeSMTP:
    """記下呼叫順序，不真的連網。"""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, local_hostname=None):
        self.host, self.port, self.helo_name = host, port, local_hostname
        self.calls: list[str] = []
        self.sent_to = None
        _FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(f"login:{user}")

    def send_message(self, msg, to_addrs=None):
        self.calls.append("send")
        self.sent_to = to_addrs

    def quit(self):
        self.calls.append("quit")


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    import smtplib
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _send(cfg):
    nc.send_email({"email_to": "someone@example.com", **cfg}, "主旨", "內文")


# --- 舊安裝不可以被影響 -------------------------------------------------

def test_config_without_mode_still_authenticates(fake_smtp):
    """沒有 smtp_mode 欄位的舊設定檔 → 照舊走 auth，行為不變。"""
    _send({"smtp_host": "mail.example.com", "smtp_username": "u",
           "smtp_password": "p", "smtp_tls": "starttls"})
    c = fake_smtp.instances[-1]
    assert c.port == 587
    assert "starttls" in c.calls
    assert any(x.startswith("login:") for x in c.calls)


# --- relay --------------------------------------------------------------

def test_relay_never_authenticates(fake_smtp):
    """relay 是靠 IP 信任 —— 就算設定檔裡殘留帳號也不該拿去認證嗎？

    這裡刻意**不**這樣要求：帳號欄位在 relay 模式下介面是收起來的，若有值
    多半是切模式前留下的。真正要釘的是「沒有帳號時不會爆掉、也不會去 login」。
    """
    _send({"smtp_mode": "relay", "smtp_host": "mail.example.com",
           "smtp_tls": "none", "smtp_port": 25})
    c = fake_smtp.instances[-1]
    assert not any(x.startswith("login:") for x in c.calls), "relay 不該認證"
    assert "starttls" not in c.calls
    assert "send" in c.calls


def test_relay_defaults_to_port_25(fake_smtp):
    """relay 慣例走 25 埠 —— 沒填就用 25，不要沿用 auth 的 587。"""
    _send({"smtp_mode": "relay", "smtp_host": "mail.example.com"})
    assert fake_smtp.instances[-1].port == 25


def test_helo_name_is_passed_through(fake_smtp):
    """有些轉送主機依 HELO 名稱決定收不收。"""
    _send({"smtp_mode": "relay", "smtp_host": "mail.example.com",
           "smtp_helo": "jtdt.corp.example"})
    assert fake_smtp.instances[-1].helo_name == "jtdt.corp.example"


def test_relay_still_requires_a_host(fake_smtp):
    with pytest.raises(RuntimeError, match="SMTP 主機"):
        _send({"smtp_mode": "relay"})


# --- direct -------------------------------------------------------------

def test_direct_looks_up_mx_and_needs_no_host(fake_smtp, monkeypatch):
    monkeypatch.setattr(nc, "_mx_hosts", lambda d: [f"mx1.{d}", f"mx2.{d}"])
    _send({"smtp_mode": "direct"})
    c = fake_smtp.instances[-1]
    assert c.host == "mx1.example.com", "應該連收件網域的第一順位 MX"
    assert c.port == 25
    assert not any(x.startswith("login:") for x in c.calls)
    assert c.sent_to == ["someone@example.com"]


def test_direct_falls_back_to_the_next_mx(fake_smtp, monkeypatch):
    """第一順位連不上要換下一台，不是整個放棄。"""
    monkeypatch.setattr(nc, "_mx_hosts", lambda d: ["bad.example.com", "good.example.com"])
    import smtplib
    real = _FakeSMTP

    def flaky(host, port, timeout=None, local_hostname=None):
        if host.startswith("bad"):
            raise OSError("connection refused")
        return real(host, port, timeout=timeout, local_hostname=local_hostname)

    monkeypatch.setattr(smtplib, "SMTP", flaky)
    _send({"smtp_mode": "direct"})
    assert fake_smtp.instances[-1].host == "good.example.com"


def test_direct_splits_recipients_by_domain(fake_smtp, monkeypatch):
    """收件者分屬不同網域要分開送，一個網域失敗不影響其他。"""
    monkeypatch.setattr(nc, "_mx_hosts", lambda d: [f"mx.{d}"])
    nc.send_email({"smtp_mode": "direct",
                    "email_to": "a@one.example, b@two.example"}, "主旨", "內文")
    hosts = sorted(c.host for c in fake_smtp.instances)
    assert hosts == ["mx.one.example", "mx.two.example"]


def test_direct_rejects_a_malformed_recipient(fake_smtp, monkeypatch):
    monkeypatch.setattr(nc, "_mx_hosts", lambda d: [f"mx.{d}"])
    with pytest.raises(RuntimeError, match="收件信箱格式"):
        nc.send_email({"smtp_mode": "direct", "email_to": "not-an-email"},
                       "主旨", "內文")


def test_direct_reports_which_domain_failed(fake_smtp, monkeypatch):
    monkeypatch.setattr(nc, "_mx_hosts", lambda d: ["nope.example"])
    import smtplib

    def always_fail(*a, **k):
        raise OSError("refused")

    monkeypatch.setattr(smtplib, "SMTP", always_fail)
    with pytest.raises(RuntimeError, match="example.com"):
        _send({"smtp_mode": "direct"})


# --- 設定的把關 ---------------------------------------------------------

def test_unknown_mode_falls_back_to_auth(tmp_path, monkeypatch):
    """沒擋的話可以塞任意字串，而寄送端是字串比對 —— 未知值會安靜落到預設分支。"""
    monkeypatch.setattr(ns, "_CACHE", None, raising=False)
    monkeypatch.setattr(ns, "_path", lambda: tmp_path / "notify.json")
    saved = ns.save({"channels": {"email": {"smtp_mode": "../../etc/passwd"}}})
    assert saved["channels"]["email"]["smtp_mode"] == "auth"


def test_helo_is_sanitised(tmp_path, monkeypatch):
    """HELO 會原樣寫進 SMTP 指令 —— 換行等於讓人往協議裡插指令。"""
    monkeypatch.setattr(ns, "_CACHE", None, raising=False)
    monkeypatch.setattr(ns, "_path", lambda: tmp_path / "notify.json")
    saved = ns.save({"channels": {"email": {
        "smtp_helo": "evil\r\nMAIL FROM:<attacker@x>"}}})
    got = saved["channels"]["email"]["smtp_helo"]
    assert "\r" not in got and "\n" not in got and " " not in got
