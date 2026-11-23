from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qa_job_bot.accounts import CurrentUser
from qa_job_bot.web_store import StoreError


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def text_values(cls, data):
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, str):
                    try:
                        value.encode("utf-8")
                    except UnicodeEncodeError:
                        raise ValueError("Некорректный текст.") from None
                    if "\0" in value:
                        raise ValueError("Некорректный текст.")
        return data


class AccountProfile(AccountInput):
    display_name: Annotated[str, Field(min_length=1, max_length=120)]


class PasswordChange(AccountInput):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    password: Annotated[str, Field(min_length=12, max_length=256)]


class TokenPassword(AccountInput):
    token: Annotated[str, Field(min_length=1, max_length=256)]
    password: Annotated[str, Field(min_length=12, max_length=256)]


class Invitation(AccountInput):
    login: Annotated[str, Field(min_length=3, max_length=100)]
    display_name: Annotated[str, Field(min_length=1, max_length=120)]


class Disabled(AccountInput):
    disabled: bool


def current_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if user is None:
        raise StoreError("Войдите в приложение.", 401)
    return user


User = Annotated[CurrentUser, Depends(current_user)]


def register_account_routes(app, accounts, session_response, cookie):
    @app.get("/api/me")
    def me(user: User):
        return asdict(user)

    @app.patch("/api/account")
    def update_profile(data: AccountProfile, user: User):
        return accounts.update_profile(user, data.display_name)

    @app.post("/api/account/password")
    def change_password(data: PasswordChange, request: Request, user: User):
        issued = accounts.change_password(user, data.current_password, data.password)
        return session_response(request, issued)

    @app.post("/api/account/sessions/revoke")
    def revoke_sessions(user: User):
        accounts.revoke_sessions(user)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(cookie)
        return response

    @app.post("/api/invitations/accept")
    def accept(data: TokenPassword, request: Request):
        issued = accounts.redeem(
            "invite",
            data.token,
            data.password,
            request.client.host if request.client else "unknown",
        )
        return session_response(request, issued)

    @app.post("/api/password-resets/accept")
    def reset(data: TokenPassword, request: Request):
        accounts.redeem(
            "reset", data.token, data.password, request.client.host if request.client else "unknown"
        )
        accounts.logout(request.cookies.get(cookie, ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(cookie)
        return response

    @app.get("/api/admin/users")
    def users(user: User):
        return {"items": accounts.list_users(user)}

    @app.post("/api/admin/invitations", status_code=201)
    def invite(data: Invitation, user: User):
        return accounts.invite(user, data.login, data.display_name)

    @app.get("/api/admin/invitations")
    def invitations(user: User):
        return {"items": accounts.invitations(user)}

    @app.delete("/api/admin/invitations/{identifier}", status_code=204)
    def revoke(identifier: str, user: User):
        accounts.revoke_invitation(user, identifier)

    @app.post("/api/admin/users/{identifier}/invitations", status_code=201)
    def reissue(identifier: str, user: User):
        return accounts.issue_access(user, identifier, "invite")

    @app.post("/api/admin/users/{identifier}/password-resets", status_code=201)
    def issue_reset(identifier: str, user: User):
        return accounts.issue_access(user, identifier, "reset")

    @app.delete("/api/admin/password-resets/{identifier}", status_code=204)
    def revoke_reset(identifier: str, user: User):
        accounts.revoke_token(user, identifier, "reset")

    @app.patch("/api/admin/users/{identifier}")
    def disable(identifier: str, data: Disabled, user: User):
        accounts.set_disabled(user, identifier, data.disabled)
        return {"disabled": data.disabled}
