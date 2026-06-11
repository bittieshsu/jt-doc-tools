"""End-to-end ACL test for pdf-stamp / pdf-watermark preview endpoints (#28 pt2).

Bug: the preview-serving endpoint (`/preview/{name}`) calls
upload_owner.require(), but the endpoints that CREATE those preview files
(`/preview`, `/preview-watermarked`, ...) never called upload_owner.record().
With auth on, an authenticated non-admin who legitimately uses the tool got a
fail-secure 403 on their OWN preview (admin passed via admin-override) — so edit
mode + composite mode showed nothing, even though the final download worked.

These tests stand up a real non-admin session (role `sales`, which grants both
pdf-stamp and pdf-watermark), upload a PDF to the preview endpoint, and assert:
  - the uploader can fetch their own preview PNG        -> 200
  - a different non-admin user cannot                   -> 403 (isolation holds)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


def _sales_client(username: str) -> TestClient:
    """Create a non-admin local user with the `sales` role (has pdf-stamp +
    pdf-watermark) and return a TestClient carrying its session cookie."""
    from app.core import user_manager, sessions, permissions
    uid = user_manager.create_local(username, username, "SalesPass1234",
                                     roles=["sales"])
    # Sanity: the role must actually grant the tools, else the middleware would
    # 403 us before we ever reach the preview ACL (masking the real assertion).
    assert permissions.user_can_use_tool(uid, "pdf-stamp")
    assert permissions.user_can_use_tool(uid, "pdf-watermark")
    token, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, token)
    return c


@pytest.mark.parametrize("tool", ["pdf-stamp", "pdf-watermark"])
def test_uploader_can_fetch_own_preview(admin_session, two_page_pdf, tool):
    owner = _sales_client(f"owner_{tool.replace('-', '_')}")
    r = owner.post(f"/tools/{tool}/preview",
                   files={"file": ("a.pdf", two_page_pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    preview_url = r.json()["preview_url"]
    # THE FIX: the owner can now fetch their own preview PNG (was 403 before).
    g = owner.get(preview_url, follow_redirects=False)
    assert g.status_code == 200, f"owner blocked from own preview: {g.status_code}"
    assert g.headers["content-type"] == "image/png"


@pytest.mark.parametrize("tool", ["pdf-stamp", "pdf-watermark"])
def test_other_user_cannot_fetch_preview(admin_session, two_page_pdf, tool):
    owner = _sales_client(f"o2_{tool.replace('-', '_')}")
    other = _sales_client(f"x2_{tool.replace('-', '_')}")
    r = owner.post(f"/tools/{tool}/preview",
                   files={"file": ("a.pdf", two_page_pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    preview_url = r.json()["preview_url"]
    # Cross-user isolation must still hold (the watermark ACL no-op is closed).
    g = other.get(preview_url, follow_redirects=False)
    assert g.status_code == 403, (
        f"another user fetched someone else's preview: {g.status_code}")


def test_auth_off_preview_served(auth_off, two_page_pdf):
    # Single-user mode: record/require are no-ops, preview just works.
    c = TestClient(app_main.app)
    r = c.post("/tools/pdf-stamp/preview",
               files={"file": ("a.pdf", two_page_pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    g = c.get(r.json()["preview_url"])
    assert g.status_code == 200
    assert g.headers["content-type"] == "image/png"
