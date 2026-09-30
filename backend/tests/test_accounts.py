from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import auth
from app.dbtypes import utcnow
from app.models import User, UserSession
from tests.conftest import PASSWORD, sign_up


async def test_registration_signs_the_visitor_in(anonymous):
    body = await sign_up(anonymous, "new@example.com")

    assert body["email"] == "new@example.com"
    assert (await anonymous.get("/api/auth/me")).status_code == 200


async def test_email_is_unique_regardless_of_case(anonymous, session):
    await sign_up(anonymous, "Casey@Example.com")

    response = await anonymous.post(
        "/api/auth/register", json={"email": "casey@EXAMPLE.com", "password": PASSWORD}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "email_taken"
    assert len((await session.scalars(select(User))).all()) == 1


async def test_database_rejects_a_duplicate_email(session):
    session.add(User(email="dup@example.com", password_hash="x"))
    await session.commit()
    session.add(User(email="dup@example.com", password_hash="y"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_short_password_is_refused(anonymous, session):
    response = await anonymous.post(
        "/api/auth/register", json={"email": "short@example.com", "password": "123456789"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["min_length"] == 10
    assert (await session.scalars(select(User))).all() == []


async def test_password_is_stored_as_argon2id_hash(anonymous, session):
    await sign_up(anonymous, "hash@example.com")
    user = await session.scalar(select(User))

    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


async def test_sign_in_and_sign_out(anonymous):
    await sign_up(anonymous, "inout@example.com")
    await anonymous.post("/api/auth/logout")
    assert (await anonymous.get("/api/auth/me")).status_code == 401

    response = await anonymous.post(
        "/api/auth/login", json={"email": "INOUT@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert (await anonymous.get("/api/portfolios")).status_code == 200


async def test_sign_out_invalidates_the_session(anonymous):
    await sign_up(anonymous, "gone@example.com")
    token = anonymous.cookies.get(auth.SESSION_COOKIE)

    assert (await anonymous.post("/api/auth/logout")).status_code == 204

    # Replaying the old cookie no longer works: the session is gone on the server.
    anonymous.cookies.set(auth.SESSION_COOKIE, token)
    assert (await anonymous.get("/api/auth/me")).status_code == 401


async def test_unknown_email_and_wrong_password_look_the_same(anonymous):
    await sign_up(anonymous, "known@example.com")
    await anonymous.post("/api/auth/logout")

    wrong = await anonymous.post(
        "/api/auth/login", json={"email": "known@example.com", "password": "wrong password"}
    )
    unknown = await anonymous.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_session_expires_after_30_idle_days(anonymous, session):
    await sign_up(anonymous, "idle@example.com")
    row = await session.scalar(select(UserSession))
    row.last_used_at = utcnow() - timedelta(days=30, minutes=1)
    await session.commit()

    assert (await anonymous.get("/api/auth/me")).status_code == 401


async def test_session_in_use_stays_valid(anonymous, session):
    await sign_up(anonymous, "active@example.com")
    row = await session.scalar(select(UserSession))
    row.last_used_at = utcnow() - timedelta(days=29)
    await session.commit()

    assert (await anonymous.get("/api/auth/me")).status_code == 200
    await session.refresh(row)
    assert utcnow() - row.last_used_at < timedelta(minutes=1)


async def test_sixth_attempt_is_refused_even_with_the_correct_password(anonymous):
    await sign_up(anonymous, "locked@example.com")
    await anonymous.post("/api/auth/logout")
    for _ in range(5):
        response = await anonymous.post(
            "/api/auth/login", json={"email": "locked@example.com", "password": "wrong password"}
        )
        assert response.status_code == 401

    response = await anonymous.post(
        "/api/auth/login", json={"email": "locked@example.com", "password": PASSWORD}
    )

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "locked_out"
    assert response.json()["detail"]["retry_at"]


async def test_lockout_ends_after_15_minutes(session):
    user = await auth.register(session, "later@example.com", PASSWORD)
    start = utcnow()
    for _ in range(5):
        with pytest.raises(auth.InvalidCredentials):
            await auth.authenticate(session, user.email, "wrong password", now=start)

    with pytest.raises(auth.LockedOut) as locked:
        await auth.authenticate(session, user.email, PASSWORD, now=start + timedelta(minutes=14))
    assert locked.value.retry_at == start + timedelta(minutes=15)

    signed_in = await auth.authenticate(
        session, user.email, PASSWORD, now=start + timedelta(minutes=15, seconds=1)
    )
    assert signed_in.id == user.id


async def test_a_success_resets_the_failure_count(session):
    user = await auth.register(session, "reset@example.com", PASSWORD)
    for _ in range(4):
        with pytest.raises(auth.InvalidCredentials):
            await auth.authenticate(session, user.email, "wrong password")
    await auth.authenticate(session, user.email, PASSWORD)
    for _ in range(4):
        with pytest.raises(auth.InvalidCredentials):
            await auth.authenticate(session, user.email, "wrong password")

    assert (await auth.authenticate(session, user.email, PASSWORD)).id == user.id


@pytest.mark.parametrize(
    "path",
    ["/api/auth/me", "/api/portfolios", "/api/assets", "/api/assets/classes",
     "/api/market/quote/1", "/api/fee-profiles", "/api/portfolios/1/orders"],
)  # fmt: skip
async def test_unauthenticated_requests_are_refused(anonymous, path):
    assert (await anonymous.get(path)).status_code == 401


async def test_account_response_has_no_password_fields(client):
    body = (await client.get("/api/auth/me")).json()

    assert set(body) == {"id", "email", "created_at"}
