"""Secrets in Windows Credential Manager, through ``keyring``.

The database only ever stores the *name* of a secret (``token_ref``), never
its value.
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "snipr"


def get(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, name) or None
    except KeyringError:
        return None


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except (PasswordDeleteError, KeyringError):
        pass


def has(name: str) -> bool:
    return get(name) is not None
