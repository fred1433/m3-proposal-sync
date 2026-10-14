"""Local credential vault, encrypted at rest with AES-256-GCM.

The vault file holds {name: base64(nonce || ciphertext)}; the entry name is the
GCM associated data, so an entry cannot be moved under another name.

Where the key lives: a 32-byte key file outside the vault file, mounted
read-only into the container (Docker secret or bind mount) and readable by the
agent user only. The agent refuses a key file that group or others can read.
This protects the vault file at rest (backups, copied volumes). It does not
protect against an administrator of the host, who can read the key file.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(Exception):
    pass


class Secret:
    """Wrapper that never prints its value."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


def create_key_file(path: str | os.PathLike) -> None:
    p = Path(path)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(AESGCM.generate_key(bit_length=256))


def _load_key(path: str | os.PathLike) -> bytes:
    p = Path(path)
    st = p.stat()
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise VaultError(f"key file {p} is accessible to group/others; chmod 400 or 600 it")
    key = p.read_bytes()
    if len(key) != 32:
        raise VaultError("key file must contain exactly 32 bytes")
    return key


class Vault:
    def __init__(self, vault_path: str | os.PathLike, key_path: str | os.PathLike) -> None:
        self.path = Path(vault_path)
        self._aead = AESGCM(_load_key(key_path))

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text("utf-8"))

    def names(self) -> list[str]:
        return sorted(self._read())

    def get(self, name: str) -> Secret:
        data = self._read()
        if name not in data:
            raise VaultError(f"secret {name!r} not in vault")
        blob = base64.b64decode(data[name])
        try:
            plain = self._aead.decrypt(blob[:12], blob[12:], name.encode())
        except Exception as exc:  # InvalidTag
            raise VaultError(f"secret {name!r} cannot be decrypted with this key") from exc
        return Secret(plain.decode("utf-8"))

    def put(self, name: str, value: str) -> None:
        data = self._read()
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, value.encode("utf-8"), name.encode())
        data[name] = base64.b64encode(nonce + ct).decode()
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
