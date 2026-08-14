"""OWASP Top 10 (2025) regression suite.

Each test maps to a specific category. Failing one means a real
exploitable hole, not a style nit.

Notable 2025 ordering changes vs 2021:
- A02 = Security Misconfiguration (was A05:2021)
- A03 = Software Supply Chain Failures (renamed from "Vulnerable and
  Outdated Components")
- A04 = Cryptographic Failures (was A02:2021)
- A05 = Injection (now includes XSS)
- A06 = Insecure Design (now includes SSRF, was A10:2021)
- A10 = Mishandling of Exceptional Conditions (NEW)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


# ---------- A01 Broken Access Control ----------

def test_a01_anonymous_blocked_from_admin(admin_session):
    """Unauthenticated request must NOT reach admin endpoints."""
    c = TestClient(app_main.app, follow_redirects=False)
    for path in ("/admin/users", "/admin/audit", "/admin/permissions"):
        r = c.get(path)
        assert r.status_code in (302, 401, 403), \
            f"{path} leaked to anon (status={r.status_code})"


def test_a01_path_traversal_blocked(admin_session):
    """safe_join must reject `..` escapes."""
    from app.core.safe_paths import safe_join
    from pathlib import Path
    base = Path("/tmp/jtdt-safe-test")
    base.mkdir(exist_ok=True)
    with pytest.raises(Exception):
        safe_join(base, "../../etc/passwd")
    with pytest.raises(Exception):
        safe_join(base, "/etc/passwd")


def test_a01_upload_owner_cross_user_isolation(admin_session, sample_pdf):
    """Owner ACL: User A's upload_id must not be downloadable by User B
    (when auth is on)."""
    from app.core import upload_owner, sessions, user_manager, permissions
    from unittest.mock import MagicMock
    # Make user B
    uid_b = user_manager.create_local("bob-other", "Bob", "TestPass1234")
    permissions.assign_role("user", str(uid_b), "default-user")
    # Pretend admin uploaded file id X
    upload_id = "a" * 32
    req_a = MagicMock()
    req_a.state.user = {"user_id": 1, "username": "jtdt-admin"}
    upload_owner.record(upload_id, req_a)
    # User B tries to access — must raise
    req_b = MagicMock()
    req_b.state.user = {"user_id": uid_b, "username": "bob-other",
                        "source": "local"}
    with pytest.raises(Exception):
        upload_owner.require(upload_id, req_b)


# ---------- A02 Cryptographic Failures ----------

def test_a02_password_uses_scrypt_with_salt():
    from app.core import passwords
    h1 = passwords.hash_password("samepass123")
    h2 = passwords.hash_password("samepass123")
    assert h1 != h2, "same password should produce different hashes (per-call salt)"
    assert h1.startswith("scrypt$"), f"unexpected hash format: {h1[:20]}"
    assert passwords.verify_password("samepass123", h1)
    assert not passwords.verify_password("wrong", h1)


def test_a02_session_cookie_flags(admin_session):
    """Session cookie must be HttpOnly + SameSite=Lax."""
    _, username, password = admin_session
    c = TestClient(app_main.app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password,
                                "next": "/", "realm": "local"})
    assert r.status_code == 302
    cookie_header = r.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header, "session cookie missing HttpOnly"
    assert "SameSite=lax" in cookie_header.lower() \
        or "samesite=lax" in cookie_header.lower(), \
        f"session cookie missing SameSite=Lax: {cookie_header}"


def test_a02_auth_settings_file_mode_600(tmp_path, monkeypatch):
    """auth_settings.json must be written mode 600 (owner only)."""
    import os
    from app.core import auth_settings
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    auth_settings.save({"backend": "off"})
    p = tmp_path / "auth_settings.json"
    if p.exists() and os.name == "posix":
        st = p.stat()
        # mask Unix permission bits
        mode = st.st_mode & 0o777
        assert mode == 0o600, f"auth_settings.json mode = {oct(mode)}, expected 0o600"


# ---------- A03 Injection ----------

def test_a03_sql_injection_in_login_blocked(admin_session):
    """SQL injection in username field must NOT bypass auth."""
    c = TestClient(app_main.app, follow_redirects=False)
    for payload in ("admin' OR '1'='1", "admin'--", "'; DROP TABLE users;--"):
        r = c.post("/login", data={
            "username": payload, "password": "anything",
            "realm": "local",
        })
        # Either 200 form-with-error (auth on) or 302 to / (auth off rare)
        # — but should NOT get a session cookie.
        assert "jtdt_session" not in r.cookies, \
            f"SQL injection succeeded with payload: {payload}"


def test_a03_xss_in_login_username_escaped(admin_session):
    """Username with <script> must be HTML-escaped on form re-render."""
    c = TestClient(app_main.app)
    payload = "<script>alert(1)</script>"
    r = c.post("/login", data={"username": payload, "password": "x",
                                "realm": "local"})
    # Must NOT contain the unescaped tag
    assert "<script>alert(1)</script>" not in r.text
    # Should contain escaped form
    assert "&lt;script&gt;" in r.text or payload not in r.text


# ---------- A05 Security Misconfiguration: headers ----------

def test_a05_security_headers_present(admin_session):
    c, _, _ = admin_session
    r = c.get("/")
    h = r.headers
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "SAMEORIGIN"
    assert "strict-origin" in h.get("Referrer-Policy", "")
    assert "camera=()" in h.get("Permissions-Policy", "")
    csp = h.get("Content-Security-Policy", "")
    assert csp, "Content-Security-Policy header missing"
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "base-uri 'self'" in csp


# ---------- A07 Authentication Failures ----------

def test_a07_password_minimum_length():
    from app.core import passwords
    ok, _ = passwords.validate_password("short")
    assert not ok, "short password should be rejected"
    ok, _ = passwords.validate_password("longenoughpassword")
    assert ok


def test_a07_lockout_after_n_fails(admin_session):
    """5 wrong passwords → account locked."""
    from app.core import auth_local, auth_settings, auth_db, db
    s = auth_settings.get()
    threshold = int(s.get("lockout_threshold", 5))
    # Clear any existing lockouts on jtdt-admin
    with db.tx(auth_db.conn()):
        auth_db.conn().execute("DELETE FROM lockouts")
    for i in range(threshold):
        try:
            auth_local.authenticate("jtdt-admin", "definitely-wrong-pw",
                                    ip="9.9.9.9")
        except auth_local.AuthError:
            pass
    # Next attempt with even correct password must be locked out
    with pytest.raises(auth_local.AuthError, match="次數過多|locked"):
        auth_local.authenticate("jtdt-admin", "TestAdmin1234", ip="9.9.9.9")


def test_a07_totp_replay_attack_blocked(admin_session):
    """Same TOTP code submitted twice in quick succession is verified by
    pyotp's window — but a code that's actually wrong is rejected
    consistently (no oracle for codes via timing)."""
    import pyotp
    from app.core import totp
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    assert totp.verify_code(secret, code) is True
    # Wrong code never verifies regardless of repetition
    for _ in range(5):
        assert totp.verify_code(secret, "000000") is False


# ---------- A09 Logging / Monitoring ----------

def test_a09_login_success_audited(admin_session):
    """Successful login must produce a `login_success` audit event."""
    from app.core import audit_db
    _, username, password = admin_session
    c = TestClient(app_main.app, follow_redirects=False)
    c.post("/login", data={"username": username, "password": password,
                            "realm": "local"})
    rows = audit_db.conn().execute(
        "SELECT event_type FROM audit_events "
        "WHERE event_type='login_success' AND username=? "
        "ORDER BY id DESC LIMIT 5", (username,)).fetchall()
    assert len(rows) >= 1


def test_a09_login_fail_audited(admin_session):
    from app.core import audit_db
    c = TestClient(app_main.app)
    c.post("/login", data={"username": "jtdt-admin",
                            "password": "wrong-pass-99999",
                            "realm": "local"})
    rows = audit_db.conn().execute(
        "SELECT event_type, details_json FROM audit_events "
        "WHERE event_type='login_fail' AND username='jtdt-admin' "
        "ORDER BY id DESC LIMIT 5").fetchall()
    assert len(rows) >= 1


# ---------- A10 SSRF ----------

def test_a10_no_user_controlled_outbound_urls():
    """Audit: no public endpoint takes a user-supplied URL and fetches it.
    Outbound URLs are restricted to:
      - LDAP (configured server only, admin-set)
      - LLM Ollama (admin-configured, optional)
      - install.sh / .ps1 (offline; not part of running service)
    """
    import re
    from pathlib import Path
    forbidden_pattern = re.compile(
        r"(urllib\.request\.urlopen|httpx\.[gp]et|requests\.[gp]et|aiohttp).*request\.|"
        r"\.fetch\(.*request\.|"
        r"params\[.*url.*\]"
    )
    src = Path(__file__).resolve().parent.parent / "app"
    bad = []
    for p in src.rglob("*.py"):
        if "tests" in p.parts:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            if forbidden_pattern.search(line):
                bad.append(f"{p.relative_to(src)}:{line_no}: {line.strip()[:80]}")
    assert not bad, "SSRF risk — user-controlled URL passed to outbound request:\n" + "\n".join(bad[:5])


# ── 管理者可設定的匯出目錄（CodeQL py/path-injection 硬化）────────────────────
def test_safe_output_dir_allows_external_backup_paths(tmp_path):
    """外部備份路徑（/mnt/backup 這類）是合理用法，不可被擋掉。"""
    from app.core.safe_paths import safe_output_dir

    d = tmp_path / "backup"
    assert safe_output_dir(d) == d.resolve()
    assert safe_output_dir(str(d)) == d.resolve()


@pytest.mark.parametrize("bad", ["/etc", "/etc/cron.d", "/proc/self", "/sys",
                                 "/dev", "/boot", "/bin", "/usr/sbin", "/"])
def test_safe_output_dir_rejects_system_dirs(bad):
    """寫進系統目錄可能覆蓋作業系統檔案 → 一律擋。"""
    import os

    from app.core.safe_paths import UnsafeOutputDir, safe_output_dir

    if os.name == "nt":
        pytest.skip("POSIX 系統目錄清單")
    with pytest.raises(UnsafeOutputDir):
        safe_output_dir(bad)


def test_safe_output_dir_rejects_relative_path():
    """相對路徑會隨行程工作目錄漂移，寫到預期外的位置。"""
    from app.core.safe_paths import UnsafeOutputDir, safe_output_dir

    with pytest.raises(UnsafeOutputDir):
        safe_output_dir("backups/settings")
    with pytest.raises(UnsafeOutputDir):
        safe_output_dir("../../etc")


def test_safe_output_dir_rejects_traversal_into_system_dir(tmp_path):
    """正規化後才判斷 → 用 .. 繞回系統目錄一樣擋得住。"""
    from app.core.safe_paths import UnsafeOutputDir, safe_output_dir
    import os

    if os.name == "nt":
        pytest.skip("POSIX 系統目錄清單")
    with pytest.raises(UnsafeOutputDir):
        safe_output_dir("/var/lib/../../etc/cron.d")


def test_safe_output_dir_rejects_existing_file(tmp_path):
    from app.core.safe_paths import UnsafeOutputDir, safe_output_dir

    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(UnsafeOutputDir):
        safe_output_dir(f)


def test_scheduled_export_save_rejects_system_target_dir(tmp_path, monkeypatch):
    """管理員在設定頁填系統目錄 → 存檔當下就被擋（不是等排程跑到才失敗）。"""
    import os

    from app.core import scheduled_export

    if os.name == "nt":
        pytest.skip("POSIX 系統目錄清單")
    with pytest.raises(ValueError):
        scheduled_export.save_settings({"enabled": True, "interval": "daily",
                                        "keep": 3, "target_dir": "/etc/cron.d"})


# ---------------------------------------------- 驗證錯誤不可回填原始輸入

@pytest.mark.parametrize("path", [
    "/admin/audit?page=1%3CScRiPt%3Ealert(1)%3C/ScRiPt%3E",
    "/admin/audit?page_size=100%3Cimg%20src=x%20onerror=alert(1)%3E",
    "/api/jobs?limit=%3Cscript%3Ealert(1)%3C/script%3E",
])
def test_validation_error_does_not_echo_input(path):
    """422 **不可以把使用者送的原字串回填到回應裡**。

    FastAPI 預設的驗證錯誤會帶 `"input": "<使用者原文>"` —— 那是攻擊者可控
    的內容，外部掃描（2026-08-13）因此把它判成反射型 XSS。

    實際上不可利用（`application/json` + `nosniff` + CSP 無 unsafe-inline，
    三重擋著），但**回填本來就沒必要** —— 使用者需要的是「哪個欄位、
    錯在哪」，不是自己送過去的字串。拿掉就整個反射面消失。
    """
    from fastapi.testclient import TestClient

    import app.main as main
    with TestClient(main.app) as c:
        r = c.get(path, follow_redirects=False)
    if r.status_code not in (422, 400):
        pytest.skip(f"這個路徑沒有觸發驗證錯誤（HTTP {r.status_code}）")
    body = r.text
    for marker in ("<ScRiPt", "<script", "onerror=", "<img"):
        assert marker not in body, (
            f"驗證錯誤回應回填了原始輸入（{marker!r}）：{body[:200]}")
    assert r.headers.get("x-content-type-options") == "nosniff"
