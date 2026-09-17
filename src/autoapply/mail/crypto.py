"""Encrypt OAuth tokens at rest (Fernet, key from AUTOAPPLY_SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path


def _key_material(project_root: Path) -> bytes:
    raw = os.environ.get("AUTOAPPLY_SECRET_KEY", "").strip()
    if not raw:
        # Generate once and persist outside git (data/ is ignored).
        key_file = project_root / "data" / ".secret_key"
        if key_file.exists():
            raw = key_file.read_text().strip()
        else:
            raw = base64.urlsafe_b64encode(os.urandom(32)).decode()
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text(raw)
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def encrypt(project_root: Path, data: bytes) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(_key_material(project_root)).encrypt(data)


def decrypt(project_root: Path, token: bytes) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(_key_material(project_root)).decrypt(token)
