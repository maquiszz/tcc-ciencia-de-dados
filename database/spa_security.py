"""Security helpers without database access or schema changes.

Rate limits are shared by threads in ONE Python process, not across workers or
servers. Use an edge/proxy limiter when deploying multiple processes. A stable,
private application secret is required to validate recovery links after restart.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from itsdangerous import BadData, URLSafeTimedSerializer


class SecurityTools:
    """Signed recovery challenges and keyed, non-reversible fingerprints."""

    def __init__(self, secret: str | bytes):
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("A non-empty application secret is required.")
        self._secret = secret
        self._serializer = URLSafeTimedSerializer(
            secret, salt="spa-panaceia-password-recovery-v1",
            signer_kwargs={"digest_method": hashlib.sha256},
        )

    def fingerprint(self, value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("Fingerprint input must be a string.")
        return hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_recovery(self, email: str, code: str) -> str:
        if not isinstance(email, str) or not isinstance(code, str) or not email.strip() or not code:
            raise ValueError("Recovery email and code are required.")
        # A signed token is readable, not encrypted: never place the OTP in it.
        return self._serializer.dumps({
            "v": 1,
            "email": self.fingerprint("recovery-email:" + email.strip().casefold()),
            "code": self.fingerprint("recovery-code:" + code),
            "nonce": secrets.token_urlsafe(16),
        })

    def verify_recovery(self, token: str, email: str, code: str, max_age: int = 900) -> bool:
        if (
            not isinstance(token, str) or not token or len(token) > 4096
            or not isinstance(email, str) or not email.strip()
            or not isinstance(code, str) or not code
            or not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0
        ):
            return False
        try:
            payload = self._serializer.loads(token, max_age=max_age)
        except (BadData, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return False
        saved_email, saved_code = payload.get("email"), payload.get("code")
        if not isinstance(saved_email, str) or not isinstance(saved_code, str):
            return False
        # Both comparisons are evaluated; malformed non-ASCII claims are rejected.
        try:
            email_matches = hmac.compare_digest(
                saved_email, self.fingerprint("recovery-email:" + email.strip().casefold())
            )
            code_matches = hmac.compare_digest(saved_code, self.fingerprint("recovery-code:" + code))
        except (TypeError, ValueError):
            return False
        return email_matches and code_matches


@dataclass
class _Bucket:
    window: float
    hits: deque[float] = field(default_factory=deque)
    expires_at: float = 0.0
    generation: int = 0


class RateLimiter:
    """Bounded, thread-safe sliding-window rate limiter.

    ``allow(key, limit, window_seconds)`` returns ``(allowed, retry_after)``.
    Successful calls consume one attempt; denied calls do not extend a ban.
    The window for a key must remain unchanged while that bucket is active.
    At capacity, unknown keys are denied instead of evicting active limits.
    Limits and windows must come from trusted application configuration.
    """

    def __init__(self, max_keys: int = 10_000, clock: Callable[[], float] | None = None):
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys <= 0:
            raise ValueError("max_keys must be a positive integer.")
        self._max_keys = max_keys
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._expiry_heap: list[tuple[float, int, str]] = []
        self._generation = 0

    def _purge(self, now: float) -> None:
        while self._expiry_heap:
            expires_at, generation, key = self._expiry_heap[0]
            bucket = self._buckets.get(key)
            if bucket is None or bucket.generation != generation:
                heapq.heappop(self._expiry_heap)
                continue
            if expires_at > now:
                break
            heapq.heappop(self._expiry_heap)
            del self._buckets[key]

    def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise ValueError("Rate-limit key must be a non-empty string of at most 1024 characters.")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer.")
        if (
            not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool)
            or not math.isfinite(window_seconds) or window_seconds <= 0
        ):
            raise ValueError("window_seconds must be finite and positive.")
        with self._lock:
            now = self._clock()
            self._purge(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    retry = math.ceil(self._expiry_heap[0][0] - now)
                    return False, max(1, retry)
                bucket = _Bucket(window=float(window_seconds))
                self._buckets[key] = bucket
            elif bucket.window != window_seconds:
                raise ValueError("An active rate-limit key cannot change its window.")
            while bucket.hits and bucket.hits[0] <= now - bucket.window:
                bucket.hits.popleft()
            if len(bucket.hits) >= limit:
                return False, max(1, math.ceil(bucket.hits[0] + bucket.window - now))
            bucket.hits.append(now)
            bucket.expires_at = now + bucket.window
            self._generation += 1
            bucket.generation = self._generation
            heapq.heappush(self._expiry_heap, (bucket.expires_at, bucket.generation, key))
            # Frequent calls can leave stale heap entries. Compact so housekeeping
            # memory remains bounded even with long-lived, frequently used keys.
            if len(self._expiry_heap) > max(64, 2 * self._max_keys):
                self._expiry_heap = [
                    (item.expires_at, item.generation, bucket_key)
                    for bucket_key, item in self._buckets.items()
                ]
                heapq.heapify(self._expiry_heap)
            return True, 0

    def clear(self) -> None:
        """Clear this process's counters; useful for isolated tests."""
        with self._lock:
            self._buckets.clear()
            self._expiry_heap.clear()

