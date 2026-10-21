"""Sign-in, first-run setup and user management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import __version__
from ..db import utcnow
from ..models import User
from ..fmt import duration
from ..security import hash_password, password_problem, verify_missing, verify_password
from ..services import audit
from . import serial
from .deps import COOKIE, admin, current_user, rt

router = APIRouter(prefix="/api/v1")


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class SetupBody(Credentials):
    name: str = Field(default="", max_length=200)


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = ""
    role: str = Field(default="viewer", pattern="^(admin|operator|viewer)$")
    password: str = Field(min_length=10, max_length=200)


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = Field(default=None, pattern="^(admin|operator|viewer)$")
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


def _set_cookie(request: Request, response: Response, user: User) -> None:
    runtime = request.app.state.rt
    token = runtime.keyring.sign_session(user.id, user.session_epoch, runtime.settings.session_hours)
    response.set_cookie(COOKIE, token, max_age=runtime.settings.session_hours * 3600, httponly=True, samesite="lax",
                        secure=runtime.settings.cookie_secure, path="/")


@router.get("/auth/status")
def status(request: Request, runtime: Any = Depends(rt)) -> dict[str, Any]:
    with runtime.db.session() as s:
        users = s.execute(select(func.count(User.id))).scalar() or 0
    out: dict[str, Any] = {"needs_setup": users == 0, "demo": runtime.settings.demo, "version": __version__}
    if runtime.settings.demo:
        from ..demo.profiles import CANDIDATES
        from ..demo.seed import DEMO_PASSWORD, DEMO_USERS

        out["demo_login"] = {"email": DEMO_USERS[0][0], "password": DEMO_PASSWORD}
        c = CANDIDATES[0]  # a simulated server that is not in the estate yet, for trying onboarding
        out["demo_onboarding"] = {"name": c.id, "host": c.host, "port": 5432, "dbname": c.databases[0], "username": "raiden_monitor", "platform": c.platform,
                                  "environment": c.environment, "version": c.version, "description": c.description, "owner": c.owner,
                                  "tags": ", ".join(f"{k}={v}" for k, v in c.tags.items()), "maintenance_window": c.maintenance_window,
                                  "ram_gb": c.mem_gb, "vcpus": c.cores, "storage": "ssd", "workload": "oltp"}
    return out


@router.post("/auth/setup")
def setup(body: SetupBody, request: Request, response: Response, runtime: Any = Depends(rt)) -> dict[str, Any]:
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    with runtime.db.session() as s:
        if (s.execute(select(func.count(User.id))).scalar() or 0) > 0:
            raise HTTPException(409, "The console is already set up. Sign in instead.")
        user = User(email=body.email.strip().lower(), name=body.name.strip(), role="admin", password_hash=hash_password(body.password), last_login_at=utcnow())
        s.add(user)
        s.flush()
        audit(s, user.email, "user.setup", user.email, "allow")
        s.expunge(user)
    _set_cookie(request, response, user)
    return serial.user(user)


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, runtime: Any = Depends(rt)) -> dict[str, Any]:
    addr, email = request.client.host if request.client else "", body.email.strip().lower()
    wait = runtime.login_throttle.wait(addr, email)
    if wait:
        with runtime.db.session() as s:
            audit(s, email, "user.login", "", "deny", ip=addr, reason="too many failed sign-ins")
        raise HTTPException(429, f"Too many failed sign-ins. Try again in {duration(wait)}.", headers={"Retry-After": str(wait)})
    with runtime.db.session() as s:
        user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
        ok = verify_password(body.password, user.password_hash) if user is not None else verify_missing(body.password)
        if ok and user is not None and not user.disabled:
            user.last_login_at = utcnow()
            audit(s, user.email, "user.login", "", "allow", ip=addr)
            s.flush()
            s.expunge(user)
        else:
            user = None
            audit(s, email, "user.login", "", "deny", ip=addr)  # stored with this session: the refusal is raised after it
    if user is None:
        runtime.login_throttle.failed(addr, email)
        raise HTTPException(401, "That email and password do not match an account.")
    runtime.login_throttle.succeeded(addr, email)
    _set_cookie(request, response, user)
    return serial.user(user)


@router.post("/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return serial.user(user)


@router.get("/users")
def users(runtime: Any = Depends(rt), _: User = Depends(admin)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        return [serial.user(u) for u in s.execute(select(User).order_by(User.id)).scalars()]


@router.post("/users")
def create_user(body: NewUser, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    with runtime.db.session() as s:
        if s.execute(select(User).where(User.email == body.email.strip().lower())).scalar_one_or_none():
            raise HTTPException(409, "An account with that email already exists.")
        u = User(email=body.email.strip().lower(), name=body.name, role=body.role, password_hash=hash_password(body.password))
        s.add(u)
        s.flush()
        audit(s, who.email, "user.create", u.email, "allow", role=body.role)
        return serial.user(u)


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404, "No such user.")
        if body.role and u.id == who.id and body.role != "admin":
            raise HTTPException(400, "You cannot remove your own admin role.")
        for k in ("name", "role", "disabled"):
            v = getattr(body, k)
            if v is not None:
                setattr(u, k, v)
        if body.password:
            problem = password_problem(body.password)
            if problem:
                raise HTTPException(400, problem)
            u.password_hash = hash_password(body.password)
            u.session_epoch += 1
        if body.disabled:
            u.session_epoch += 1
        audit(s, who.email, "user.update", u.email, "allow", **{k: v for k, v in body.model_dump().items() if v is not None and k != "password"})
        return serial.user(u)


@router.delete("/users/{uid}")
def delete_user(uid: int, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    if uid == who.id:
        raise HTTPException(400, "You cannot delete your own account.")
    with runtime.db.session() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404, "No such user.")
        audit(s, who.email, "user.delete", u.email, "allow")
        s.delete(u)
    return {"ok": True}
