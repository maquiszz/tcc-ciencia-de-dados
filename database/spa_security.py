"""Security helpers for authentication, rate limiting, and session revocation.

Rate limits and in-memory session revocations are shared by threads in ONE
Python process, not across workers or servers. Use an edge/proxy limiter when
deploying multiple processes. A stable, private application secret is required
to validate recovery links after restart.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import hmac
import math
import secrets
import threading
import time
from collections import OrderedDict, deque
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

    def hash_otp(self, email: str, code: str) -> str:
        """Compute a keyed HMAC for an activation OTP bound to the target email."""
        normalized = (email or "").strip().casefold()
        return "v2:" + self.fingerprint(f"otp-code:{normalized}:{code}")

    def hash_otp_compact(self, email: str, code: str) -> str:
        """Compute a 6-character keyed HMAC tag ('~' + 5 base64url chars) for legacy VARCHAR(6) columns."""
        normalized = (email or "").strip().casefold()
        raw = hmac.new(self._secret, f"otp-code:{normalized}:{code}".encode("utf-8"), hashlib.sha256).digest()
        return "~" + base64.urlsafe_b64encode(raw).decode("ascii")[:5]

    def verify_otp(self, stored_otp: str | None, email: str, code: str) -> bool:
        """Verify an activation OTP against v2 HMAC, compact 6-char HMAC, or legacy plaintext."""
        if not isinstance(stored_otp, str) or not stored_otp or not isinstance(code, str) or not code:
            return False
        if stored_otp.startswith("v2:"):
            expected = self.hash_otp(email, code)
            return hmac.compare_digest(stored_otp, expected)
        if stored_otp.startswith("~") and len(stored_otp) == 6:
            expected_compact = self.hash_otp_compact(email, code)
            return hmac.compare_digest(stored_otp, expected_compact)
        return hmac.compare_digest(stored_otp, code)

    def encode_recovery_record(
        self, email: str, code: str, expires_at_ts: int, nonce_id: str | None = None
    ) -> tuple[str, str]:
        """Encode a hashed recovery challenge with persistent expiration timestamp."""
        if not isinstance(email, str) or not isinstance(code, str) or not email.strip() or not code:
            raise ValueError("Recovery email and code are required.")
        nid = nonce_id or secrets.token_urlsafe(12)
        normalized = email.strip().casefold()
        code_hash = self.fingerprint(f"recovery-db:{normalized}:{nid}:{code}")
        return f"v2:{int(expires_at_ts)}:{nid}:{code_hash}", nid

    def verify_recovery_record(
        self, stored_value: str | None, email: str, code: str, now_epoch: float | None = None
    ) -> tuple[bool, bool, str | None]:
        """Validate database-persisted recovery record.

        Returns ``(is_valid, is_expired, nonce_id)``.
        """
        if not isinstance(stored_value, str) or not stored_value or not isinstance(code, str) or not code:
            return False, False, None
        now = time.time() if now_epoch is None else float(now_epoch)
        if stored_value.startswith("v2:"):
            parts = stored_value.split(":", 3)
            if len(parts) != 4:
                return False, False, None
            _, exp_str, nid, saved_hash = parts
            try:
                exp_ts = int(exp_str)
            except ValueError:
                return False, False, None
            if now > exp_ts:
                return False, True, nid
            normalized = (email or "").strip().casefold()
            expected_hash = self.fingerprint(f"recovery-db:{normalized}:{nid}:{code}")
            matches = hmac.compare_digest(saved_hash, expected_hash)
            return matches, False, nid
        # Legacy plaintext fallback for in-flight records prior to migration
        matches = hmac.compare_digest(stored_value, code)
        return matches, False, None

    def issue_recovery(self, email: str, code: str, nonce_id: str | None = None) -> str:
        if not isinstance(email, str) or not isinstance(code, str) or not email.strip() or not code:
            raise ValueError("Recovery email and code are required.")
        payload = {
            "v": 1,
            "email": self.fingerprint("recovery-email:" + email.strip().casefold()),
            "code": self.fingerprint("recovery-code:" + code),
            "nonce": secrets.token_urlsafe(16),
        }
        if nonce_id:
            payload["nid"] = nonce_id
        # A signed token is readable, not encrypted: never place the OTP in it.
        return self._serializer.dumps(payload)

    def verify_recovery(
        self,
        token: str,
        email: str,
        code: str,
        max_age: int = 900,
        expected_nonce_id: str | None = None,
    ) -> bool:
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
        if expected_nonce_id is not None:
            token_nid = payload.get("nid")
            if not isinstance(token_nid, str) or not hmac.compare_digest(token_nid, expected_nonce_id):
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
    limit: int = 1
    hits: deque[float] = field(default_factory=deque)
    expires_at: float = 0.0
    generation: int = 0
    last_seen: float = 0.0


class RateLimiter:
    """Bounded, thread-safe sliding-window rate limiter with LRU eviction of unblocked keys.

    ``allow(key, limit, window_seconds)`` returns ``(allowed, retry_after)``.
    Successful calls consume one attempt; denied calls do not extend a ban.
    The window for a key must remain unchanged while that bucket is active.
    At capacity, expired buckets are purged first; if still full, the oldest
    non-exhausted bucket (below its limit) is evicted so random/fake keys cannot
    cause a Denial of Service for legitimate users while active rate-limit bans
    remain strictly enforced.
    """

    def __init__(self, max_keys: int = 10_000, clock: Callable[[], float] | None = None):
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys <= 0:
            raise ValueError("max_keys must be a positive integer.")
        self._max_keys = max_keys
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
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

    def _evict_unblocked_lru(self, now: float) -> bool:
        """Evict the least-recently-used bucket that is not currently rate-limited."""
        for candidate_key, candidate in list(self._buckets.items()):
            while candidate.hits and candidate.hits[0] <= now - candidate.window:
                candidate.hits.popleft()
            if not candidate.hits:
                del self._buckets[candidate_key]
                return True
            if len(candidate.hits) < candidate.limit:
                del self._buckets[candidate_key]
                return True
        return False

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
                    if not self._evict_unblocked_lru(now):
                        retry = math.ceil(self._expiry_heap[0][0] - now) if self._expiry_heap else 1
                        return False, max(1, retry)
                bucket = _Bucket(window=float(window_seconds), limit=limit, last_seen=now)
                self._buckets[key] = bucket
            elif bucket.window != window_seconds:
                raise ValueError("An active rate-limit key cannot change its window.")
            else:
                bucket.limit = limit
                bucket.last_seen = now
                self._buckets.move_to_end(key)

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

    def reset_key(self, key: str) -> None:
        """Remove a specific rate-limit key."""
        with self._lock:
            self._buckets.pop(key, None)

    def clear(self) -> None:
        """Clear this process's counters; useful for isolated tests."""
        with self._lock:
            self._buckets.clear()
            self._expiry_heap.clear()


class SessionRevocationStore:
    """Bounded, thread-safe server-side session revocation tracker.

    Tracks individually revoked session IDs (``sid``) as well as per-user
    revocation timestamps/generations so stolen signed cookies are rejected
    immediately after logout or global session revocation.
    """

    def __init__(self, max_entries: int = 20_000, clock: Callable[[], float] | None = None):
        if not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer.")
        self._max_entries = max_entries
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._revoked_sids: OrderedDict[str, float] = OrderedDict()
        self._user_revoked_at: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def _purge(self, now: float) -> None:
        expired_sids = [sid for sid, exp in self._revoked_sids.items() if exp <= now]
        for sid in expired_sids:
            del self._revoked_sids[sid]
        expired_users = [u for u, (_, exp) in self._user_revoked_at.items() if exp <= now]
        for u in expired_users:
            del self._user_revoked_at[u]

    def revoke_sid(self, sid: str, ttl_seconds: float = 43_200.0) -> None:
        if not isinstance(sid, str) or not sid:
            return
        with self._lock:
            now = self._clock()
            self._purge(now)
            if sid in self._revoked_sids:
                self._revoked_sids.move_to_end(sid)
            elif len(self._revoked_sids) >= self._max_entries:
                self._revoked_sids.popitem(last=False)
            self._revoked_sids[sid] = now + max(1.0, float(ttl_seconds))

    def revoke_user(self, user_key: str, ttl_seconds: float = 43_200.0) -> float:
        if not isinstance(user_key, str) or not user_key.strip():
            return 0.0
        normalized = user_key.strip().casefold()
        with self._lock:
            now = self._clock()
            self._purge(now)
            if normalized in self._user_revoked_at:
                self._user_revoked_at.move_to_end(normalized)
            elif len(self._user_revoked_at) >= self._max_entries:
                self._user_revoked_at.popitem(last=False)
            self._user_revoked_at[normalized] = (now, now + max(1.0, float(ttl_seconds)))
            return now

    def is_revoked(
        self,
        sid: str | None,
        user_key: str | None = None,
        issued_at: float | None = None,
    ) -> bool:
        with self._lock:
            now = self._clock()
            self._purge(now)
            if isinstance(sid, str) and sid in self._revoked_sids:
                return True
            if isinstance(user_key, str) and user_key.strip():
                normalized = user_key.strip().casefold()
                record = self._user_revoked_at.get(normalized)
                if record is not None:
                    revoked_at, _ = record
                    if issued_at is None or float(issued_at) <= revoked_at:
                        return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._revoked_sids.clear()
            self._user_revoked_at.clear()


@dataclass
class _AdaptiveClient:
    hits: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0
    strikes: int = 0
    persisted_during_block: bool = False
    last_seen: float = 0.0


class AdaptiveIPBlocker:
    """Thread-safe burst protection with an exponentially growing IP ban.

    The first burst above ``limit`` is blocked for ``base_block_seconds``.
    Requests made while blocked mark the client as persistent. Once the current
    punishment has elapsed, a persistent client is immediately blocked again
    for twice as long. This avoids multiplying the punishment for every packet
    in the same millisecond while still escalating a continuing attack.

    State lives in one Python process. For several workers or servers, the same
    rule should also be configured at the reverse proxy/CDN edge.
    """

    _MAX_RETRY_AFTER = 2_147_483_647

    def __init__(
        self,
        limit: int = 30,
        window_seconds: float = 1.0,
        base_block_seconds: int = 30,
        max_keys: int = 20_000,
        clock: Callable[[], float] | None = None,
    ):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer.")
        if (
            not isinstance(window_seconds, (int, float))
            or isinstance(window_seconds, bool)
            or not math.isfinite(window_seconds)
            or window_seconds <= 0
        ):
            raise ValueError("window_seconds must be finite and positive.")
        if (
            not isinstance(base_block_seconds, int)
            or isinstance(base_block_seconds, bool)
            or base_block_seconds <= 0
        ):
            raise ValueError("base_block_seconds must be a positive integer.")
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys <= 0:
            raise ValueError("max_keys must be a positive integer.")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.base_block_seconds = base_block_seconds
        self._max_keys = max_keys
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._clients: dict[str, _AdaptiveClient] = {}

    def _retry_after(self, blocked_until: float, now: float) -> int:
        if math.isfinite(blocked_until):
            return max(1, min(self._MAX_RETRY_AFTER, math.ceil(blocked_until - now)))
        return self._MAX_RETRY_AFTER

    def _start_block(self, client: _AdaptiveClient, now: float) -> tuple[bool, int, int]:
        client.strikes += 1
        # Python integers do not overflow. If converting the exponentially large
        # duration to the monotonic float eventually does, the ban becomes final.
        duration = self.base_block_seconds * (1 << (client.strikes - 1))
        try:
            client.blocked_until = now + duration
        except OverflowError:
            client.blocked_until = math.inf
        if not math.isfinite(client.blocked_until):
            client.blocked_until = math.inf
        client.persisted_during_block = False
        client.hits.clear()
        return False, self._retry_after(client.blocked_until, now), client.strikes

    def _make_room(self, now: float) -> bool:
        if len(self._clients) < self._max_keys:
            return True
        removable = [
            (client.last_seen, key)
            for key, client in self._clients.items()
            if client.blocked_until <= now
        ]
        if not removable:
            return False
        _, oldest_key = min(removable)
        del self._clients[oldest_key]
        return True

    def check(self, key: str) -> tuple[bool, int, int]:
        """Return ``(allowed, retry_after_seconds, strike_number)``."""
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise ValueError("Block key must be a non-empty string of at most 1024 characters.")
        with self._lock:
            now = self._clock()
            client = self._clients.get(key)
            if client is None:
                if not self._make_room(now):
                    return False, self.base_block_seconds, 1
                client = _AdaptiveClient(last_seen=now)
                self._clients[key] = client
            client.last_seen = now

            if client.blocked_until > now:
                client.persisted_during_block = True
                return False, self._retry_after(client.blocked_until, now), client.strikes

            if client.blocked_until and client.persisted_during_block:
                return self._start_block(client, now)

            if client.blocked_until:
                # The client respected the whole ban, so a later isolated burst
                # starts again at 30 seconds instead of carrying a lifetime mark.
                client.blocked_until = 0.0
                client.strikes = 0
                client.hits.clear()

            cutoff = now - self.window_seconds
            while client.hits and client.hits[0] <= cutoff:
                client.hits.popleft()
            if len(client.hits) >= self.limit:
                return self._start_block(client, now)
            client.hits.append(now)
            return True, 0, client.strikes

    def clear(self) -> None:
        with self._lock:
            self._clients.clear()
