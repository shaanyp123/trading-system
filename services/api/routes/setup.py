"""services/api/routes/setup.py — Phase 0 first-run bootstrap endpoint.

Backend-spec §4.1.1:

    POST /api/setup/verify-token
    Body:     SetupTokenVerifyRequest  {token: str}
    Response: SetupTokenVerifyResponse {valid, intended_role, ceremony_session_id}

The raw setup token is generated at first boot (see `services/api/main.py`
lifespan) and printed once to the api container's stdout. The operator copies
that token, navigates to the apex domain, and submits it here. On a successful
Argon2id-verify we mark it consumed (single-use) and return a short-lived
`ceremony_session_id` that the WebAuthn registration flow will consume in a
follow-on Phase-0 PR. Day 5 ships the verification endpoint; the WebAuthn
ceremony lands when `services/api/routes/webauthn.py` is authored.

Failure mode: any failure (token not found, expired, already consumed, hash
mismatch) returns 401 with `INVALID_SETUP_TOKEN`. We do NOT reveal which of
those four reasons applied — the operator either holds a valid token or
they don't, and a granular error surface only helps an attacker.
"""

from __future__ import annotations

from typing import Literal

import structlog
import uuid_utils as uuid7_lib
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from services.api.db import get_session
from services.api.errors import AppError
from services.api.repos.setup_tokens import (
    PostgresSetupTokenRepo,
    SetupTokenRepo,
)

log = structlog.get_logger()

router = APIRouter()


class SetupTokenVerifyRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)


class SetupTokenVerifyResponse(BaseModel):
    valid: Literal[True]
    intended_role: Literal["owner", "reader"]
    ceremony_session_id: str


def _get_repo(session: AsyncSession = Depends(get_session)) -> SetupTokenRepo:
    """Override-able dep for unit tests."""
    return PostgresSetupTokenRepo(session)


@router.post(
    "/api/setup/verify-token",
    tags=["setup"],
    response_model=SetupTokenVerifyResponse,
)
async def verify_token(
    body: SetupTokenVerifyRequest,
    repo: SetupTokenRepo = Depends(_get_repo),
) -> SetupTokenVerifyResponse:
    row = await repo.consume_if_valid(body.token)
    if row is None:
        log.warning("setup_token_verify_failed")
        raise AppError(
            error_code="INVALID_SETUP_TOKEN",
            message="Setup token is invalid, expired, or already consumed.",
            status_code=401,
        )

    ceremony_session_id = str(uuid7_lib.uuid7())
    log.info(
        "setup_token_verified",
        token_uuid=str(row.token_uuid),
        intended_role=row.intended_role,
        ceremony_session_id=ceremony_session_id,
    )
    return SetupTokenVerifyResponse(
        valid=True,
        intended_role=row.intended_role,
        ceremony_session_id=ceremony_session_id,
    )
