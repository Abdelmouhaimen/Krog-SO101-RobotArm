"""Bearer-token authentication for the HTTP API (ADR-018).

Tokens are random 256-bit secrets shown ONCE when created. Only their SHA-256 hashes are stored, in a
JSON file OUTSIDE the repository (default ``~/.config/so101-companion/api-tokens.json``, mode 0600).
Verification compares hashes in constant time. The file is re-read when it changes, so a revoked token
stops working without a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

TOKEN_PREFIX = "so101_"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "so101-companion" / "api-tokens.json"


class TokenFileError(RuntimeError):
    pass


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def resolve_token_file(configured: str) -> Path:
    env = os.environ.get("SO101_API_TOKEN_FILE")
    if env:
        return Path(env).expanduser()
    return Path(configured).expanduser() if configured else DEFAULT_TOKEN_FILE


def create_token(path: Path, name: str) -> str:
    """Generate a token, store its hash (file 0600, directory 0700) and return the token (show once)."""
    if not name or len(name) > 64:
        raise TokenFileError("token name must be 1..64 characters")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    entries = _read(path) if path.exists() else []
    entries.append(
        {
            "name": name,
            "hash": token_hash(token),
            "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"schema_version": 1, "tokens": entries}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return token


def _read(path: Path) -> list[dict[str, str]]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise TokenFileError(f"cannot read API token file {path}: {exc}") from exc
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    if not isinstance(tokens, list) or not all(
        isinstance(t, dict) and isinstance(t.get("hash"), str) and t["hash"].startswith("sha256:")
        for t in tokens
    ):
        raise TokenFileError(f"API token file {path} is malformed")
    return tokens


class TokenVerifier:
    """Checks presented bearer tokens against stored hashes (a fixed set, or a file re-read on change)."""

    def __init__(self, *, path: Path | None = None, hashes: Iterable[str] = ()) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._hashes: dict[str, str] = {h: "static" for h in hashes}
        self._mtime: float | None = None
        if path is not None:
            self._reload()
            if not self._hashes:
                raise TokenFileError(f"API token file {path} contains no tokens")

    def _reload(self) -> None:
        assert self.path is not None
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            raise TokenFileError(
                f"no API token file at {self.path} — create one with: so101-companion --create-api-token NAME"
            ) from exc
        if mtime == self._mtime:
            return
        if self.path.stat().st_mode & 0o077:
            raise TokenFileError(f"API token file {self.path} must not be readable by others (chmod 600)")
        self._hashes = {t["hash"]: t.get("name", "") for t in _read(self.path)}
        self._mtime = mtime

    def verify(self, token: str | None) -> str | None:
        """The token's name if valid, else None. Constant-time comparison against every stored hash."""
        if not token:
            return None
        presented = token_hash(token)
        with self._lock:
            if self.path is not None:
                try:
                    self._reload()
                except TokenFileError:
                    return None  # unreadable file: fail closed
            match = None
            for stored, name in self._hashes.items():
                if hmac.compare_digest(stored, presented):
                    match = name or "token"
            return match
