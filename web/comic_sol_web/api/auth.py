"""FastAPI routes for the Web authentication boundary."""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from comic_sol_web.auth import AuthError, AuthService, SessionPrincipal, require_principal


def _service_from(source: Any, request: Request) -> AuthService:
    service = source(request) if callable(source) else source
    if not isinstance(service, AuthService):
        raise RuntimeError("authentication service is unavailable")
    return service


def create_local_session_router(service_source: Any) -> APIRouter:
    """Create the local-only bootstrap endpoint for a fixed local principal."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/local-session")
    async def local_session(request: Request) -> JSONResponse:
        host = request.client.host if request.client is not None else ""
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local session rejected")
        service = _service_from(service_source, request)
        authenticated = service.create_session(
            SessionPrincipal("comic-sol-local-user", "local")
        )
        response = JSONResponse(
            {"user_id": authenticated.principal.user_id, "login": authenticated.principal.login}
        )
        service.set_session_cookies(response, authenticated)
        return response

    return router


def create_auth_router(
    service: AuthService,
    *,
    callback_url: str,
    post_login_url: str = "/",
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.get("/login")
    async def login() -> RedirectResponse:
        _, binding, authorization_url = service.begin_oauth(callback_url)
        response = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
        service.set_oauth_binding_cookie(response, binding)
        return response

    @router.get("/callback")
    async def callback(request: Request, state: str, code: str) -> RedirectResponse:
        try:
            authenticated = await service.complete_oauth(
                state=state,
                binding=request.cookies.get(service.oauth_binding_cookie_name),
                code=code,
                redirect_uri=callback_url,
            )
        except AuthError as error:
            raise HTTPException(status_code=400, detail="OAuth callback rejected") from error
        response = RedirectResponse(post_login_url, status_code=status.HTTP_303_SEE_OTHER)
        service.clear_oauth_binding_cookie(response)
        service.set_session_cookies(response, authenticated)
        return response

    @router.get("/session")
    async def session(request: Request) -> dict[str, str]:
        principal = await require_principal(request)
        return {"user_id": principal.user_id, "login": principal.login}

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        try:
            service.require_csrf(request)
        except AuthError as error:
            raise HTTPException(status_code=403, detail="CSRF validation failed") from error
        service.revoke(request.cookies.get(service.session_cookie_name))
        response = JSONResponse({"ok": True})
        service.clear_session_cookies(response)
        return response

    return router


create_router = create_auth_router
