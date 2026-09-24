"""設定檔裡的祕密（金鑰、密碼）加解密。

全站用**同一把**金鑰（`data/.session_secret`，Fernet）。語音服務、SSO、通知
三組設定各自有一份一模一樣的實作（`jtlw_settings` / `sso_settings` /
`notify_settings` 的 `encrypt_secret` / `decrypt_secret`）；新的設定一律用這一支，
不要再抄第五份。

解不開（金鑰換過、檔案是別台機器的）時回空字串並記一行 —— 呼叫端會把它
當成「沒設定」，管理員重新輸入就好，不要讓整頁因此壞掉。
"""
from __future__ import annotations

import base64

from ..logging_setup import get_logger

logger = get_logger(__name__)


def _fernet():
    from cryptography.fernet import Fernet
    from . import auth_settings
    return Fernet(base64.urlsafe_b64encode(auth_settings._ensure_secret()))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, *, label: str = "") -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        logger.warning("secret_box: %s 解不開（金鑰換過，或設定檔來自別台機器？）",
                       label or "祕密欄位")
        return ""
