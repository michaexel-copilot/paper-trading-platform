from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api.deps import error
from app.api.schemas import Credentials, UserOut
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

BAD_CREDENTIALS = "Email address or password is incorrect."


def _secure(request: Request) -> bool:
    return request.app.state.settings.cookie_secure


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    if len(body.password) < auth.MIN_PASSWORD_LENGTH:
        raise error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "password_too_short",
            f"The password must be at least {auth.MIN_PASSWORD_LENGTH} characters long.",
            min_length=auth.MIN_PASSWORD_LENGTH,
        )
    try:
        user = await auth.register(session, body.email, body.password)
    except auth.EmailTaken:
        raise error(
            status.HTTP_409_CONFLICT, "email_taken", "This email address is already in use."
        ) from None
    auth.set_session_cookie(response, await auth.start_session(session, user), _secure(request))
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    try:
        user = await auth.authenticate(session, body.email, body.password)
    except auth.LockedOut as locked:
        raise error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "locked_out",
            "Too many failed sign-in attempts. Try again later.",
            retry_at=locked.retry_at.isoformat(),
        ) from None
    except auth.InvalidCredentials:
        raise error(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", BAD_CREDENTIALS) from None
    auth.set_session_cookie(response, await auth.start_session(session, user), _secure(request))
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        await auth.end_session(session, token)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(auth.current_user)):
    return user
