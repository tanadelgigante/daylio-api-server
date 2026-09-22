"""Authentication placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/api/auth.py
Scope    : Fase 0 placeholder. Phase 5 implements PBKDF2 hashing (Werkzeug),
           signed sessions (itsdangerous), API keys for automation and the
           ``@admin_required`` decorator (D6).
================================================================================
"""

from __future__ import annotations


def hash_password(password: str) -> str:
    """Hash a plaintext password (implemented in Phase 5)."""
    raise NotImplementedError("hash_password() is implemented in Phase 5 (WORKPLAN §7)")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a hash (implemented in Phase 5)."""
    raise NotImplementedError("verify_password() is implemented in Phase 5 (WORKPLAN §7)")
