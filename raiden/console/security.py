"""Passwords (scrypt), signed session cookies, encrypted secrets at rest (Fernet) and host-agent tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest)))
        return hmac.compare_digest(calc, _unb64(digest))
    except (ValueError, TypeError):
        return False


_DUMMY: list[str] = []


def verify_missing(password: str) -> bool:
    """For an unknown email: the same scrypt work as a real check, so the answer time does not tell who has an account."""
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(16)))
    verify_password(password, _DUMMY[0])
    return False


class LoginThrottle:
    """Failed sign-ins, kept in memory (one console per state store): after 5 failures for one account from one address,
    or 20 from one address, within 15 minutes, that address waits until the oldest failure is 15 minutes old."""

    WINDOW = 900
    LIMITS = {"pair": 5, "addr": 20}

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}

    def _keys(self, addr: str, email: str) -> list[tuple[str, int]]:
        return [(f"pair:{addr}:{email}", self.LIMITS["pair"]), (f"addr:{addr}", self.LIMITS["addr"])]

    def wait(self, addr: str, email: str, now: float | None = None) -> int:
        """Seconds until this address may try this account again (0: now)."""
        now = time.time() if now is None else now
        longest = 0.0
        for key, limit in self._keys(addr, email):
            recent = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
            self._fails[key] = recent
            if len(recent) >= limit:
                longest = max(longest, recent[-limit] + self.WINDOW - now)
        return int(longest) + (1 if longest else 0)

    def failed(self, addr: str, email: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key, _ in self._keys(addr, email):
            self._fails.setdefault(key, []).append(now)
        if len(self._fails) > 10_000:  # a scan from many addresses must not grow memory without limit
            for key in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.WINDOW]:
                del self._fails[key]

    def succeeded(self, addr: str, email: str) -> None:
        self._fails.pop(f"pair:{addr}:{email}", None)


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.isdigit():
        return "Use letters as well as digits."
    return None


# ---------------------------------------------------------------------------------------------------- keys
class Keyring:
    """One master secret: signs session cookies and derives the key that encrypts stored passwords and API keys."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()
        self._sign_key = hashlib.sha256(b"raiden-session:" + self._secret).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"raiden-secrets:" + self._secret).digest()))

    @classmethod
    def load(cls, configured: str, data_dir: Path) -> "Keyring":
        if configured:
            return cls(configured)
        path = data_dir / "secret.key"
        if not path.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(48))
        return cls(path.read_text().strip())

    # -- secrets at rest -------------------------------------------------------------------------------------
    def encrypt(self, plaintext: str | None) -> str | None:
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None  # the master key changed: the secret must be entered again

    # -- sessions ---------------------------------------------------------------------------------------------
    def sign_session(self, user_id: int, epoch: int, hours: int) -> str:
        payload = _b64(json.dumps({"u": user_id, "e": epoch, "x": int(time.time()) + hours * 3600, "n": secrets.token_hex(6)}).encode())
        mac = _b64(hmac.new(self._sign_key, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{mac}"

    def read_session(self, token: str) -> tuple[int, int] | None:
        try:
            payload, mac = token.split(".", 1)
        except ValueError:
            return None
        good = _b64(hmac.new(self._sign_key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, mac):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if int(data.get("x", 0)) < time.time():
            return None
        return int(data["u"]), int(data.get("e", 0))


# ---------------------------------------------------------------------------------------------------- agent tokens
def new_agent_token() -> tuple[str, str]:
    """(token shown once to the operator, hash stored in the database)."""
    token = "rdn_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
