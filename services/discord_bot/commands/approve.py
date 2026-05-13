"""services/discord_bot/commands/approve.py — ``/approve <signal_id>`` slash command.

Post-pivot follow-up #1 (closes PR #138's "Discord-side approval surface"
ask). The operator sees a signal embed in `#signals` (emitted by the
api's signal-emit SSE → webhook_pusher fan-out per PR #107), copies the
signal_id from the embed footer, and runs `/approve <signal_id>` in
any channel.

The flow:

  * Args: ``signal_id: str`` — operator-typed UUID. Validated client-side
    before the api round-trip (better UX than a 422 from FastAPI).
  * Confirmation: ephemeral embed + ``[✓ Approve] [✗ Cancel]`` buttons.
    On ✓: POST `/api/signals/{signal_id}/approve` with no
    `override_size` (uses the planner-emitted target_contracts).
  * Response: success embed with audit_event_uuid + audit_sequence_no
    pointer ("watch `#fills` for the broker confirmation"). On error:
    operator-actionable embed for ``SIGNAL_NOT_FOUND`` / ``SIGNAL_NOT_PENDING``;
    canonical envelope render for other errors.
  * Audit: ``signal_approved`` event written by the api's
    ``services.risk.signal_dispatch.apply_signal_dispatch`` with
    ``actor=operator_via_discord`` (today the api uses the bot's
    SessionContext for the actor field).

Scope intentionally narrow — only ``/approve``:

  * Reject + defer require a ``DecisionDiaryEntry`` (10-2000 char
    reasoning text + 5-value enum tag). Multi-line input in slash-command
    UX is awkward; the web `DecisionDiaryModal` is the right surface.
    Phase 1+ can add `/reject` + `/defer` with a modal-style interaction
    if operator demand emerges.
  * No `override_size` — operator can't pass a quantity override from
    Discord today. The planner-emitted target is what gets approved.
    Phase 1+ enhancement: add `--size` arg if the operator needs it.

Confirm-button architecture — mirror of
:class:`services.discord_bot.commands.halt.HaltConfirmView`:

  * ``discord.ui.View`` with two buttons: ✓ (style=success — green;
    contrast with halt's ``danger`` red so the operator doesn't confuse
    the action class at a glance) and ✗ (style=secondary).
  * View has a 60s timeout — after which the buttons no longer fire.
  * Only the invoker can click confirm/cancel
    (``interaction_check`` validates ``interaction.user.id``).
  * Consumed-once flag protects against fast-double-click races.
"""

from __future__ import annotations

from uuid import UUID

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_INFO,
    build_approve_confirm_embed,
    build_approve_error_embed,
    build_approve_invalid_uuid_embed,
    build_approve_success_embed,
)

log = structlog.get_logger()


#: Confirm-view timeout in seconds. Mirrors halt's
#: ``_CONFIRM_VIEW_TIMEOUT_SECONDS`` (60s) — long enough for a deliberate
#: read-then-decide pause; short enough that a stale ephemeral message
#: doesn't stay clickable after the operator's intent has gone stale.
_CONFIRM_VIEW_TIMEOUT_SECONDS: float = 60.0


def _is_uuid(value: str) -> bool:
    """Soft UUID format check before the api round-trip.

    Returns True iff ``UUID(value)`` succeeds — handles both hyphenated
    and unhyphenated forms; FastAPI's path-param parser is more permissive
    so this is a UX gate, not a security gate.
    """
    try:
        UUID(value)
    except (ValueError, TypeError):
        return False
    return True


class ApproveConfirmView(discord.ui.View):
    """Ephemeral confirm-or-cancel view for ``/approve <signal_id>``.

    Same state-machine shape as
    :class:`services.discord_bot.commands.halt.HaltConfirmView`:

      * Pristine → operator clicks ✗ → cancelled (edit message to
        "Cancelled.") OR operator clicks ✓ → invoked (api call →
        success/error embed).
      * Pristine → 60s timeout → buttons disabled (no api call).

    Only the original invoker can click. The view does NOT persist
    across bot restarts (in-memory only) — a restart between
    ``/approve`` invocation and confirm leaves the operator with a stale
    view; they re-issue ``/approve``.
    """

    def __init__(
        self,
        *,
        api_client: ApiClient,
        invoker_id: int,
        signal_id: str,
        environment: str,
    ) -> None:
        super().__init__(timeout=_CONFIRM_VIEW_TIMEOUT_SECONDS)
        self._api_client = api_client
        self._invoker_id = invoker_id
        self._signal_id = signal_id
        self._environment = environment
        self._consumed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the original invoker can click confirm/cancel.

        Single-operator deployment makes this defensive against a
        guild-share scenario where another member sees the ephemeral —
        ephemerals are visible only to the invoker by Discord's design,
        so this check is belt-and-suspenders.
        """
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                "This confirmation is for the operator who invoked `/approve`. "
                "Re-issue the command to approve.",
                ephemeral=True,
            )
            return False
        if self._consumed:
            await interaction.response.send_message(
                "This confirmation has already been used. Re-issue `/approve` to approve.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="approve_confirm",
        emoji="✅",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ApproveConfirmView],
    ) -> None:
        self._consumed = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info(
            "discord_approve_confirmed",
            user_id=interaction.user.id,
            signal_id=self._signal_id,
        )
        try:
            response = await self._api_client.approve_signal(self._signal_id)
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_approve_api_error",
                status_code=exc.status_code,
                error_code=exc.error_code,
                signal_id=self._signal_id,
            )
            embed = build_approve_error_embed(environment=self._environment, error=exc)
        else:
            log.info(
                "discord_approve_succeeded_via_api",
                signal_id=self._signal_id,
                audit_event_uuid=response.audit_event_uuid,
                audit_sequence_no=response.audit_sequence_no,
                new_status=response.new_status,
            )
            embed = build_approve_success_embed(environment=self._environment, response=response)
        # Disable both buttons so they can't be re-clicked.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="approve_cancel",
        emoji="✖️",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ApproveConfirmView],
    ) -> None:
        self._consumed = True
        log.info(
            "discord_approve_cancelled",
            user_id=interaction.user.id,
            signal_id=self._signal_id,
        )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Approve cancelled",
                description="System unchanged. No api call was made.",
                color=EMBED_COLOR_INFO,
            ),
            view=self,
        )


def register_approve(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    environment: str,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/approve <signal_id>`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="approve",
        description=(
            "Approve a pending signal by ID. Confirms before invoking; "
            "OrderPlacementWorker forwards to IBKR within ~5s."
        ),
        guild=guild,
    )
    @app_commands.describe(
        signal_id="Signal UUID (copy from #signals channel embed footer).",
    )
    async def approve(interaction: discord.Interaction, signal_id: str) -> None:
        signal_id = signal_id.strip()
        # Client-side UUID format check — gives a friendlier error than
        # a 422 from FastAPI's path-param parser.
        if not _is_uuid(signal_id):
            embed = build_approve_invalid_uuid_embed(raw_signal_id=signal_id)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        log.info(
            "discord_command_invoked",
            command="/approve",
            user_id=interaction.user.id,
            signal_id=signal_id,
        )
        embed = build_approve_confirm_embed(signal_id=signal_id, environment=environment)
        view = ApproveConfirmView(
            api_client=api_client,
            invoker_id=interaction.user.id,
            signal_id=signal_id,
            environment=environment,
        )
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )


__all__ = ["ApproveConfirmView", "register_approve"]
