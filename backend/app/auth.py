"""Accounts, sign-in and sessions."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dbtypes import utcnow
from app.models import LoginAttempt, User, UserSession

SESSION_COOKIE = "session"
SESSION_IDLE_LIMIT = timedelta(days=30)
# last_used_at is refreshed at most this often, to avoid a write per request.
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)
MIN_PASSWORD_LENGTH = 10
MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

_hasher = PasswordHasher()  # argon2id
# Verified when the email is unknown, so both failure paths cost the same.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class EmailTaken(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class LockedOut(Exception):
    def __init__(self, retry_at: datetime):
        self.retry_at = retry_at


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


async def register(session: AsyncSession, email: str, password: str) -> User:
    user = User(
        email=normalize_email(email),
        password_hash=await asyncio.to_thread(_hasher.hash, password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailTaken from exc
    return user


async def _lockout_until(session: AsyncSession, email: str, now: datetime) -> datetime | None:
    failures = list(
        await session.scalars(
            select(LoginAttempt)
            .where(LoginAttempt.email == email)
            .order_by(LoginAttempt.attempted_at)
        )
    )
    if len(failures) < MAX_FAILED_ATTEMPTS:
        return None
    retry_at = failures[MAX_FAILED_ATTEMPTS - 1].attempted_at + LOCKOUT
    if now < retry_at:
        return retry_at
    # The lockout has run out; counting starts again.
    await session.execute(delete(LoginAttempt).where(LoginAttempt.email == email))
    await session.commit()
    return None


async def authenticate(
    session: AsyncSession, email: str, password: str, now: datetime | None = None
) -> User:
    now = now or utcnow()
    email = normalize_email(email)
    retry_at = await _lockout_until(session, email, now)
    if retry_at is not None:
        raise LockedOut(retry_at)

    user = await session.scalar(select(User).where(User.email == email))
    password_hash = user.password_hash if user else _DUMMY_HASH
    valid = await asyncio.to_thread(_verify, password_hash, password)
    if user is None or not valid:
        session.add(LoginAttempt(email=email, attempted_at=now))
        await session.commit()
        raise InvalidCredentials

    await session.execute(delete(LoginAttempt).where(LoginAttempt.email == email))
    await session.commit()
    return user


async def start_session(session: AsyncSession, user: User, now: datetime | None = None) -> str:
    """Create a session and return its token. Only the token's hash is stored."""
    now = now or utcnow()
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token_hash=_token_hash(token), user_id=user.id, created_at=now, last_used_at=now
        )
    )
    await session.commit()
    return token


async def end_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
    await session.commit()


async def user_for_token(
    session: AsyncSession, token: str, now: datetime | None = None
) -> User | None:
    now = now or utcnow()
    row = await session.scalar(
        select(UserSession).where(UserSession.token_hash == _token_hash(token))
    )
    if row is None:
        return None
    if now - row.last_used_at > SESSION_IDLE_LIMIT:
        await session.delete(row)
        await session.commit()
        return None
    if now - row.last_used_at > SESSION_TOUCH_INTERVAL:
        row.last_used_at = now
        await session.commit()
    return await session.get(User, row.user_id)


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_IDLE_LIMIT.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    user = await user_for_token(session, token) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user
