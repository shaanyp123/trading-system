"""services/discord_bot/commands/capital.py — ``/capital-deposit`` +
``/capital-withdraw`` slash commands.

Per cutover plan §7 + frontend-spec §6.x (TBD; commands not yet
formally locked in spec but the cutover plan §10 step 20 explicitly
expects ``/capital-deposit 25000 'initial live funding'`` to work).
Mirrors ``services/discord_bot/commands/halt.py``'s pattern:

  * Args: ``amount`` (numeric > 0) + ``reason`` (1-500 chars).
  * Confirmation: ephemeral embed + ✓ / ✗ buttons (60s timeout,
    invoker-only, single-use).
  * On ✓: POST ``/api/system/capital-event`` with the event_type
    derived from the slash-command name.
  * Response: success embed with capital_event_id + threshold_met +
    audit UUIDs; or error embed with the canonical envelope.

Side effects:
  * INSERT ``capital_events`` row.
  * Audit row ``capital_event_deposit`` or ``capital_event_withdrawal``.
  * If amount >= 5% of pre-event equity (or first deposit on $0):
    audit row ``capital_event_mode_started`` + UPDATE risk_state mode
    fields (capital_event_active_until_session_no = +30,
    capital_event_vol_normalized_at_session_no = +5).
  * Deposit (threshold-met): drawdown baseline resets to post-event
    equity.
  * Withdrawal: drawdown baseline unchanged.

No SSE emit on the bot side; the api route handler logs structured
events and the audit chain carries the durable record. A web /system
page refresh shows the new capital event.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_INFO,
    build_capital_event_confirm_embed,
    build_capital_event_error_embed,
    build_capital_event_success_embed,
)

log = structlog.get_logger()


_CONFIRM_VIEW_TIMEOUT_SECONDS: float = 60.0


class CapitalEventConfirmView(discord.ui.View):
    """Ephemeral confirm-or-cancel view for ``/capital-deposit`` +
    ``/capital-withdraw``. Same shape as :class:`HaltConfirmView`."""

    def __init__(
        self,
        *,
        api_client: ApiClient,
        invoker_id: int,
        event_type: Literal["deposit", "withdrawal"],
        amount_usd: str,
        reason: str,
        environment: str,
    ) -> None:
        super().__init__(timeout=_CONFIRM_VIEW_TIMEOUT_SECONDS)
        self._api_client = api_client
        self._invoker_id = invoker_id
        self._event_type = event_type
        self._amount_usd = amount_usd
        self._reason = reason
        self._environment = environment
        self._consumed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                f"This confirmation is for the operator who invoked "
                f"`/capital-{self._event_type}`. Re-issue the command "
                "to record a capital event.",
                ephemeral=True,
            )
            return False
        if self._consumed:
            await interaction.response.send_message(
                f"This confirmation has already been used. Re-issue "
                f"`/capital-{self._event_type}` to record.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm",
        style=discord.ButtonStyle.success,
        custom_id="capital_event_confirm",
        emoji="✅",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[CapitalEventConfirmView],
    ) -> None:
        self._consumed = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.warning(
            "discord_capital_event_confirmed",
            user_id=interaction.user.id,
            event_type=self._event_type,
            amount_usd=self._amount_usd,
            reason=self._reason,
        )
        try:
            result = await self._api_client.invoke_capital_event(
                event_type=self._event_type,
                amount_usd=self._amount_usd,
                reason=self._reason,
            )
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_capital_event_api_error",
                event_type=self._event_type,
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            embed = build_capital_event_error_embed(
                environment=self._environment,
                event_type=self._event_type,
                error=exc,
            )
        else:
            log.warning(
                "discord_capital_event_recorded_via_api",
                event_type=self._event_type,
                capital_event_id=result.capital_event_id,
                threshold_met=result.threshold_met,
            )
            embed = build_capital_event_success_embed(
                environment=self._environment,
                event_type=self._event_type,
                amount_usd=self._amount_usd,
                threshold_met=result.threshold_met,
                post_event_equity=result.post_event_equity,
                capital_event_audit_event_uuid=result.capital_event_audit_event_uuid,
                mode_started_audit_event_uuid=result.mode_started_audit_event_uuid,
            )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="capital_event_cancel",
        emoji="✖️",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[CapitalEventConfirmView],
    ) -> None:
        self._consumed = True
        log.info(
            "discord_capital_event_cancelled",
            user_id=interaction.user.id,
            event_type=self._event_type,
        )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"Capital {self._event_type} cancelled",
                description="No api call was made. System unchanged.",
                color=EMBED_COLOR_INFO,
            ),
            view=self,
        )


def _validate_amount(amount: float) -> tuple[str | None, str | None]:
    """Validate the slash-command-supplied amount. Returns
    ``(amount_str, error_message)`` — one is None.

    Discord's app_commands API constrains float types client-side, but
    we still apply server-side defense for negative/zero/NaN.
    """
    try:
        amount_dec = Decimal(str(amount))
    except (ArithmeticError, InvalidOperation):
        return None, f"Amount `{amount}` is not a valid number."
    if amount_dec <= 0:
        return None, f"Amount must be positive; got `{amount_dec}`."
    if amount_dec.is_nan() or amount_dec.is_infinite():
        return None, f"Amount `{amount}` is NaN or Infinity."
    # Render at most 2 decimals (USD cents). Trailing-zero-strip the
    # rendering so '25000' stays clean, '25000.50' keeps the cents.
    quantized = amount_dec.quantize(Decimal("0.01"))
    amount_str = str(quantized.normalize()) if quantized != 0 else "0"
    if "E" in amount_str:
        # Scientific notation can sneak in from .normalize() on integer
        # Decimal — render plain.
        amount_str = f"{quantized:f}"
    return amount_str, None


async def _send_amount_error(interaction: discord.Interaction, error_msg: str) -> None:
    await interaction.response.send_message(error_msg, ephemeral=True)


def _validate_reason(reason: str) -> tuple[str | None, str | None]:
    reason = reason.strip()
    if not reason:
        return None, "Reason is required (1-500 chars)."
    if len(reason) > 500:
        return None, f"Reason is too long ({len(reason)} chars; max 500)."
    return reason, None


def register_capital_event_commands(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    environment: str,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach ``/capital-deposit`` + ``/capital-withdraw`` slash commands."""

    @tree.command(
        name="capital-deposit",
        description=(
            "Record a deposit (+capital). >= 5% of equity triggers the "
            "30-session capital_event mode."
        ),
        guild=guild,
    )
    @app_commands.describe(
        amount="Deposit amount in USD (positive number).",
        reason="Why the deposit? 1-500 chars (operator-readable in audit).",
    )
    async def capital_deposit(interaction: discord.Interaction, amount: float, reason: str) -> None:
        await _handle_capital_event_command(
            interaction=interaction,
            event_type="deposit",
            amount=amount,
            reason=reason,
            api_client=api_client,
            environment=environment,
        )

    @tree.command(
        name="capital-withdraw",
        description=(
            "Record a withdrawal (-capital). Drawdown baseline unchanged; "
            "mode still activates if >= 5%."
        ),
        guild=guild,
    )
    @app_commands.describe(
        amount="Withdrawal amount in USD (positive number).",
        reason="Why the withdrawal? 1-500 chars (operator-readable in audit).",
    )
    async def capital_withdraw(
        interaction: discord.Interaction, amount: float, reason: str
    ) -> None:
        await _handle_capital_event_command(
            interaction=interaction,
            event_type="withdrawal",
            amount=amount,
            reason=reason,
            api_client=api_client,
            environment=environment,
        )


async def _handle_capital_event_command(
    *,
    interaction: discord.Interaction,
    event_type: Literal["deposit", "withdrawal"],
    amount: float,
    reason: str,
    api_client: ApiClient,
    environment: str,
) -> None:
    """Shared handler for both slash commands."""
    amount_str, amount_err = _validate_amount(amount)
    if amount_err is not None:
        await _send_amount_error(interaction, amount_err)
        return
    assert amount_str is not None  # narrowed by amount_err check

    reason_clean, reason_err = _validate_reason(reason)
    if reason_err is not None:
        await _send_amount_error(interaction, reason_err)
        return
    assert reason_clean is not None  # narrowed by reason_err check

    log.info(
        "discord_command_invoked",
        command=f"/capital-{event_type}",
        user_id=interaction.user.id,
        amount_usd=amount_str,
    )
    embed = build_capital_event_confirm_embed(
        event_type=event_type,
        amount_usd=amount_str,
        reason=reason_clean,
        environment=environment,
    )
    view = CapitalEventConfirmView(
        api_client=api_client,
        invoker_id=interaction.user.id,
        event_type=event_type,
        amount_usd=amount_str,
        reason=reason_clean,
        environment=environment,
    )
    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True,
    )


__all__ = [
    "CapitalEventConfirmView",
    "register_capital_event_commands",
]
