"""Request dependencies: the runtime, the signed-in user, role checks and the CSRF header."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, HTTPException, Request

from ..models import User

COOKIE = "raiden_session"
ROLES = {"viewer": 1, "operator": 2, "admin": 3}


def rt(request: Request) -> Any:
    return request.app.state.rt


def current_user(request: Request) -> User:
    runtime = request.app.state.rt
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    data = runtime.keyring.read_session(token)
    if data is None:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    uid, epoch = data
    with runtime.db.session() as s:
        user = s.get(User, uid)
        if user is None or user.disabled or user.session_epoch != epoch:
            raise HTTPException(401, "Your session has ended. Sign in again.")
        s.expunge(user)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "raiden":
        raise HTTPException(403, "Missing the X-Requested-With header (cross-site requests are refused).")
    return user


def require(role: str) -> Callable[..., User]:
    def dep(user: User = Depends(current_user)) -> User:
        if ROLES.get(user.role, 0) < ROLES[role]:
            raise HTTPException(403, f"This needs the {role} role; you are a {user.role}.")
        return user

    return dep


viewer = require("viewer")
operator = require("operator")
admin = require("admin")
