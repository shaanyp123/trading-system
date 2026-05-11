"""services/discord_bot/commands/halt.py — `/halt <reason>` slash command.

Frontend-spec §6.3 ``/halt`` (Phase 0+; LOCKED — risk-tightening only,
no Discord-side resume per spec §6.1):

  * Args: ``reason: str`` (required, 1-200 chars per spec; api enforces
    1-500 via ``KillSwitchInvokeRequest`` — bot uses spec's tighter
    bound for operator-friendly Discord-side error messages).
  * Confirmation: ephemeral embed + ``[Halt now] [Cancel]`` buttons.
    On ✓: POST ``/api/system/kill-switch/invoke`` with
    ``trigger="manual_judgment"``; reason verbatim.
  * Response: success embed (post-Week-4-Wed dispatcher) OR error embed
    with the canonical envelope (today's reality: 501
    ``KILL_SWITCH_HANDLER_NOT_WIRED``).
  * Audit: ``kill_switch_invoked`` event with
    ``actor=operator_via_discord`` (api-side; the bot doesn't write
    audit_log directly — A02 binding stays intact).

Resume-via-Discord rejection: spec §6.3 mandates the bot reply
"Risk-loosening — web + WebAuthn UV required. Open the web app to
resume." The api-side enforces this as well. Day 23 doesn't register a
``/resume`` command at all (matches spec §6.3 — "Not registered as
slash command (avoids autocomplete temptation)"); the bot ONLY exposes
``/halt`` for risk transitions.

Confirm-button architecture:

  * ``discord.ui.View`` with two buttons: ✓ (style=danger to discourage
    accidental clicks) and ✗ (style=secondary).
  * View has a 60s timeout — after which the buttons are disabled and
    the operator must re-issue ``/halt`` if they still want to halt.
    Discord's ephemeral message stays visible to the operator after
    timeout but the buttons no longer fire.
  * The view is keyed to the original interaction's user — only the
    invoker can click confirm/cancel (anyone else gets a "this isn't
    for you" rejection ephemeral). Single-operator system means this is
    defensive against guild-share scenarios; the operator's solo guild
    is the production deployment.
"""

from __future__ import annotations

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_INFO,
    build_halt_confirm_embed,
    build_halt_invoke_error_embed,
    build_halt_invoke_success_embed,
)

log = structlog.get_logger()


#: Confirm-view timeout in seconds. Long enough for a deliberate
#: read-then-decide pause; short enough that a stale ephemeral message
#: doesn't stay clickable after the operator's intent has gone stale.
_CONFIRM_VIEW_TIMEOUT_SECONDS: float = 60.0


class HaltConfirmView(discord.ui.View):
    """Ephemeral confirm-or-cancel view for ``/halt``.

    Owns a single state machine:
      * Pristine → operator clicks ✗ → cancelled (edit message to
        "Cancelled.") OR operator clicks ✓ → invoked (api call →
        success/error embed).
      * Pristine → 60s timeout → buttons disabled (no api call).

    Only the original invoker can click. The view does NOT persist
    across bot restarts (in-memory only) — a restart between
    ``/halt`` invocation and confirm leaves the operator with a stale
    view; they re-issue ``/halt``.
    """

    def __init__(
        self,
        *,
        api_client: ApiClient,
        invoker_id: int,
        reason: str,
        environment: str,
    ) -> None:
        super().__init__(timeout=_CONFIRM_VIEW_TIMEOUT_SECONDS)
        self._api_client = api_client
        self._invoker_id = invoker_id
        self._reason = reason
        self._environment = environment
        self._consumed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the original invoker can click confirm/cancel."""
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                "This confirmation is for the operator who invoked `/halt`. "
                "Re-issue the command to halt the system.",
                ephemeral=True,
            )
            return False
        if self._consumed:
            # Defense-in-depth: discord.py disables the view after the
            # first interaction completes, but a fast double-click could
            # still race two button presses; this lock prevents the second
            # from re-firing the api call.
            await interaction.response.send_message(
                "This confirmation has already been used. Re-issue `/halt` to halt.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm halt",
        style=discord.ButtonStyle.danger,
        custom_id="halt_confirm",
        emoji="🛑",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[HaltConfirmView],
    ) -> None:
        self._consumed = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.warning(
            "discord_halt_confirmed",
            user_id=interaction.user.id,
            reason=self._reason,
        )
        try:
            result = await self._api_client.invoke_kill_switch(
                trigger="manual_judgment",
                reason=self._reason,
            )
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_halt_api_error",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            embed = build_halt_invoke_error_embed(
                environment=self._environment,
                error=exc,
            )
        else:
            log.warning(
                "discord_halt_invoked_via_api",
                audit_event_uuid=result.audit_event_uuid,
                new_state=result.new_state,
            )
            embed = build_halt_invoke_success_embed(
                environment=self._environment,
                audit_event_uuid=result.audit_event_uuid,
                new_state=result.new_state,
                severity=result.severity,
            )
        # Disable both buttons so they can't be re-clicked.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="halt_cancel",
        emoji="✖️",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[HaltConfirmView],
    ) -> None:
        self._consumed = True
        log.info("discord_halt_cancelled", user_id=interaction.user.id)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Halt cancelled",
                description="System unchanged. No api call was made.",
                color=EMBED_COLOR_INFO,
            ),
            view=self,
        )


def register_halt(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    environment: str,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/halt <reason>`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="halt",
        description=(
            "Invoke the kill switch (HALT_NEW). Risk-tightening; "
            "RESUME requires WebAuthn re-auth via web."
        ),
        guild=guild,
    )
    @app_commands.describe(reason="Why halt? 1-200 chars (operator-readable in audit log).")
    async def halt(interaction: discord.Interaction, reason: str) -> None:
        # Spec §6.3 bound: 1-200 chars. Tighter than the api's 1-500 to
        # give a Discord-side rejection BEFORE the api round-trip when
        # the operator slips and pastes a paragraph.
        reason = reason.strip()
        if not reason:
            await interaction.response.send_message(
                "Reason is required (1-200 chars).",
                ephemeral=True,
            )
            return
        if len(reason) > 200:
            await interaction.response.send_message(
                f"Reason is too long ({len(reason)} chars; max 200). Tighten and retry.",
                ephemeral=True,
            )
            return

        log.info("discord_command_invoked", command="/halt", user_id=interaction.user.id)
        embed = build_halt_confirm_embed(reason=reason, environment=environment)
        view = HaltConfirmView(
            api_client=api_client,
            invoker_id=interaction.user.id,
            reason=reason,
            environment=environment,
        )
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )


__all__ = ["HaltConfirmView", "register_halt"]
