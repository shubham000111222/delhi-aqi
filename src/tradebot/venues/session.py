"""Broker sessions: tokens that expire daily, and a login that needs a human.

An exchange API session is not a long-lived API key. It is obtained through OAuth plus
2FA, it expires at the end of the trading day, and the login usually cannot be completed
unattended. Treating that as an infrastructure detail is how a bot ends up unable to
square off at 15:20.

This module owns the lifecycle: is the token still valid, when does it expire, and what
should happen when it cannot be renewed. It never prints or logs the token.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.clock import utc


class SessionError(RuntimeError):
    """The session is not usable and could not be renewed."""


@dataclass
class Token:
    value: str
    issued_at: datetime
    expires_at: datetime

    def valid(self, now: datetime, *, margin: timedelta = timedelta(0)) -> bool:
        return utc(now) + margin < self.expires_at

    def seconds_left(self, now: datetime) -> float:
        return (self.expires_at - utc(now)).total_seconds()

    def __repr__(self) -> str:  # never leak the secret into a traceback or a log line
        return f"Token(expires_at={self.expires_at.isoformat()})"


class SessionManager(ABC):
    """Renewable session with a daily expiry."""

    name = "session"

    @abstractmethod
    async def login(self) -> Token: ...

    @abstractmethod
    def expiry_for(self, now: datetime) -> datetime: ...

    async def ensure(self, now: datetime, *, margin: timedelta = timedelta(minutes=5)) -> Token:
        """Return a usable token, renewing it when it is close to expiry."""
        if self._token is None or not self._token.valid(now, margin=margin):
            self._token = await self.login()
        return self._token

    @property
    def token(self) -> Token | None:
        return self._token

    _token: Token | None = None


class StaticSession(SessionManager):
    """Crypto: a key pair that does not expire, so there is nothing to renew."""

    name = "static"

    def __init__(self, token: str = "api-key-pair", ttl: timedelta = timedelta(days=3650)) -> None:
        self._static = token
        self._ttl = ttl
        self._token: Token | None = None

    def expiry_for(self, now: datetime) -> datetime:
        return utc(now) + self._ttl

    async def login(self) -> Token:
        now = utc(datetime.now().astimezone())
        return Token(value=self._static, issued_at=now, expires_at=now + self._ttl)


@dataclass
class LoginAttempt:
    at: datetime
    ok: bool
    reason: str = ""


@dataclass
class InteractiveSession(SessionManager):
    """A broker that needs a human: OAuth + 2FA, re-login at the start of each day.

    ``login_fn`` is injected so this is testable without a broker, and so the real
    implementation can do whatever the broker requires (TOTP prompt, browser redirect)
    without this class knowing about it.
    """

    name: str = "interactive"
    login_fn: object | None = None
    close_time_utc: tuple[int, int] = (10, 0)  # NSE closes 15:30 IST = 10:00 UTC
    attempts: list[LoginAttempt] = field(default_factory=list)
    _token: Token | None = None

    def expiry_for(self, now: datetime) -> datetime:
        """Tokens die at the end of the trading day, not 24 h after issue."""
        now = utc(now)
        end_of_day = now.replace(hour=self.close_time_utc[0], minute=self.close_time_utc[1], second=0, microsecond=0)
        if end_of_day <= now:
            end_of_day = end_of_day + timedelta(days=1)
        return end_of_day

    async def login(self) -> Token:
        if self.login_fn is None:
            raise SessionError(
                "no login function configured: an interactive broker session needs OAuth + 2FA"
            )
        now = utc(datetime.now().astimezone())
        try:
            value = self.login_fn()  # type: ignore[operator]
            if hasattr(value, "__await__"):
                value = await value
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            self.attempts.append(LoginAttempt(at=now, ok=False, reason=str(exc)[:200]))
            raise SessionError(f"login failed: {exc}") from exc
        token = Token(value=str(value), issued_at=now, expires_at=self.expiry_for(now))
        self.attempts.append(LoginAttempt(at=now, ok=True))
        return token

    def can_trade_unattended(self, now: datetime) -> bool:
        """False when the token is gone and only a human can replace it."""
        return self._token is not None and self._token.valid(now)
