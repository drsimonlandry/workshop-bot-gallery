"""Credential encryption, token hashing, URL (SSRF) validation, and usage limits."""

import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

# Fleet agents are served from LangGraph Platform hosts, e.g. <name>.us.langgraph.app.
ALLOWED_HOST_SUFFIXES = (".langgraph.app",)

MAX_MESSAGE_CHARS = 2000

_SECRET_PATTERNS = [
    re.compile(r"lsv2_[A-Za-z0-9_]+"),
    re.compile(r"(?i)(x-api-key|authorization|api[_-]?key|token)\s*[:=]\s*\S+"),
]


class SecurityError(Exception):
    """Raised with a message that is safe to show to users."""


class RateLimitError(SecurityError):
    pass


# --- Credential encryption -------------------------------------------------

def generate_encryption_key() -> str:
    return Fernet.generate_key().decode()


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise SecurityError("Encryption key is not configured correctly.") from None


def encrypt_secret(plaintext: str, key: str) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, key: str) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise SecurityError("Stored credential could not be decrypted.") from None


# --- Management tokens -----------------------------------------------------

def generate_management_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Tokens are 256-bit random values, so a plain SHA-256 is sufficient.
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)


def check_admin_secret(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


# --- API URL validation (SSRF) --------------------------------------------

def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def validate_api_url(url: str, resolver=_resolve) -> str:
    """Return a normalized Fleet API URL or raise SecurityError.

    Requires HTTPS, an allowlisted Fleet hostname, and public resolved addresses.
    """
    bad = SecurityError("The API URL must be an https:// Fleet deployment URL (ending in .langgraph.app).")
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        raise bad from None
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise bad
    if port not in (None, 443):
        raise bad
    try:
        ipaddress.ip_address(host)
        raise bad  # raw IPs are never allowed
    except ValueError:
        pass
    if not any(host.endswith(s) and len(host) > len(s) for s in ALLOWED_HOST_SUFFIXES):
        raise bad
    try:
        addresses = resolver(host)
    except OSError:
        raise SecurityError("Couldn't resolve the API URL. Check it and try again.") from None
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise bad
    return f"https://{host}"


# --- Redaction -------------------------------------------------------------

def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


# --- Usage limits ----------------------------------------------------------

class UsageLimiter:
    """In-process sliding-window and concurrency limits.

    Streamlit runs every session in one process, so this is enforced server-side.
    It resets on app restart, which is acceptable for a workshop.
    """

    def __init__(self, per_visitor=10, per_bot=30, window_s=60, max_concurrent_per_bot=3, clock=time.monotonic):
        self.per_visitor = per_visitor
        self.per_bot = per_bot
        self.window_s = window_s
        self.max_concurrent_per_bot = max_concurrent_per_bot
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._active: dict[str, int] = defaultdict(int)

    def _count(self, key) -> int:
        q = self._hits[key]
        cutoff = self._clock() - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        return len(q)

    @contextmanager
    def acquire(self, visitor_id: str, bot_id: str):
        with self._lock:
            if self._count(("v", visitor_id)) >= self.per_visitor:
                raise RateLimitError("You're sending messages too quickly. Wait a moment and try again.")
            if self._count(("b", bot_id)) >= self.per_bot:
                raise RateLimitError("This bot is busy right now. Try again in a minute.")
            if self._active[bot_id] >= self.max_concurrent_per_bot:
                raise RateLimitError("This bot is handling other conversations. Try again in a moment.")
            now = self._clock()
            self._hits[("v", visitor_id)].append(now)
            self._hits[("b", bot_id)].append(now)
            self._active[bot_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._active[bot_id] -= 1


def validate_message(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise SecurityError("Type a message first.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise SecurityError(f"Messages can be up to {MAX_MESSAGE_CHARS} characters.")
    return text
