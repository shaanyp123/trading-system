"""Unit tests for ``services.discord_bot.commands.*`` — slash command handlers.

Tests use a stub ``ApiClient`` (no live HTTP) and mocked
``discord.Interaction`` (no live gateway). Pure Python; no dpytest, no
testcontainers; no live api or live Discord.

A22 N/A; A27 N/A here (live Discord runtime exercised at Day 23 carryover);
A06 enforced (every datetime tz-aware UTC).

Coverage matrix:

  * ``/positions`` — happy path → defer + followup with positions embed
  * ``/positions`` — api error → defer + followup with error embed
  * ``/halt <reason>`` — empty/whitespace reason → ephemeral rejection
  * ``/halt <reason>`` — too-long reason → ephemeral rejection
  * ``/halt <reason>`` — happy path → confirm view sent
  * HaltConfirmView ✓ button — bot calls api → success embed on 200
  * HaltConfirmView ✓ button — api 501 → KILL_SWITCH_HANDLER_NOT_WIRED
    error embed
  * HaltConfirmView ✗ button — message edited to "cancelled"
  * HaltConfirmView interaction_check — non-invoker rejected
  * HaltConfirmView — second click after consume → rejected
  * ``/status`` — happy path → defer + followup with status embed
  * ``/status`` — api error → defer + followup with error embed
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
    CapitalEventInvokeResponse,
    HealthCheck,
    HealthResponse,
    KillSwitchInvokeResponse,
    PositionsResponse,
    SignalApproveResponse,
)
from services.discord_bot.commands.approve import ApproveConfirmView, register_approve
from services.discord_bot.commands.capital import (
    CapitalEventConfirmView,
    _validate_amount,
    _validate_reason,
    register_capital_event_commands,
)
from services.discord_bot.embeds import EMBED_COLOR_INFO, EMBED_COLOR_OK
from services.discord_bot.commands.halt import HaltConfirmView, register_halt
from services.discord_bot.commands.positions import register_positions
from services.discord_bot.commands.status import register_status

# ---------------------------------------------------------------------------
# Fixtures: stub ApiClient + mock Interaction
# ---------------------------------------------------------------------------


def _stub_api_client() -> ApiClient:
    """Construct a real ApiClient instance and replace its async methods.

    The bot code uses ``ApiClient`` as a typed contract; we don't subclass
    or mock the entire class, just replace the four async methods with
    AsyncMocks that the tests configure per-case.
    """
    client = ApiClient(
        base_url="http://stub:8000",
        bearer_token="stub-token",
        timeout_seconds=1.0,
    )
    client.get_health = AsyncMock()  # type: ignore[method-assign]
    client.get_positions_current = AsyncMock()  # type: ignore[method-assign]
    client.invoke_kill_switch = AsyncMock()  # type: ignore[method-assign]
    client.approve_signal = AsyncMock()  # type: ignore[method-assign]
    client.invoke_capital_event = AsyncMock()  # type: ignore[method-assign]
    return client


@pytest.fixture
async def stub_client() -> ApiClient:
    client = _stub_api_client()
    yield client
    await client.aclose()


def _build_mock_interaction(*, user_id: int = 100) -> MagicMock:
    """Build a mocked discord.Interaction with the methods our handlers call."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id

    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()

    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    return interaction


def _empty_positions_response() -> PositionsResponse:
    return PositionsResponse(
        positions=[],
        as_of=datetime(2026, 5, 15, 21, 35, tzinfo=UTC),
    )


def _ok_health_response() -> HealthResponse:
    return HealthResponse(
        status="ok",
        environment="paper",
        version="abc1234",
        db_connected=True,
        checks=[HealthCheck(name="postgres", ok=True, latency_ms=4.21)],
    )


def _build_tree() -> app_commands.CommandTree[discord.Client]:
    """Build a CommandTree without instantiating a real discord.Client.

    ``CommandTree.__init__`` reads ``client.http`` + ``client._connection``
    AND checks ``client._connection._command_tree is None`` (rejecting
    duplicate trees). A bare MagicMock returns truthy for any attribute,
    so we must explicitly set ``_command_tree = None`` to bypass the
    duplicate check.
    """
    client = MagicMock()
    client.http = MagicMock()
    state = MagicMock()
    state._command_tree = None
    client._connection = state
    return app_commands.CommandTree(client)


def _command_callback(tree: app_commands.CommandTree[discord.Client], name: str) -> Any:
    """Pull the registered command's callback function from the tree.

    discord.py wraps the decorated function in an ``app_commands.Command``;
    the underlying function is exposed at ``.callback``. We invoke the
    callback directly (not through the gateway) so tests don't need to
    spin up a real bot.
    """
    cmd = tree.get_command(name)
    assert cmd is not None, f"command /{name} not registered"
    # mypy: tree.get_command returns Command[...]|Group; we know it's a leaf Command
    return cast(app_commands.Command[Any, Any, Any], cmd).callback


# ---------------------------------------------------------------------------
# /positions
# ---------------------------------------------------------------------------


class TestPositionsCommand:
    async def test_happy_path_defers_then_followup(
        self,
        stub_client: ApiClient,
    ) -> None:
        stub_client.get_positions_current.return_value = _empty_positions_response()  # type: ignore[attr-defined]
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "positions")
        interaction = _build_mock_interaction()

        await callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        stub_client.get_positions_current.assert_awaited_once()  # type: ignore[attr-defined]
        interaction.followup.send.assert_awaited_once()
        sent_kwargs = interaction.followup.send.await_args.kwargs
        assert sent_kwargs["ephemeral"] is True
        embed = sent_kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert "Open positions" in (embed.title or "")

    async def test_api_error_renders_error_embed(
        self,
        stub_client: ApiClient,
    ) -> None:
        stub_client.get_positions_current.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=503,
            error_code="SERVICE_UNAVAILABLE",
            message="db down",
        )
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "positions")
        interaction = _build_mock_interaction()

        await callback(interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "SERVICE_UNAVAILABLE" in (embed.title or "")
        assert "db down" in (embed.description or "")


# ---------------------------------------------------------------------------
# /halt input validation
# ---------------------------------------------------------------------------


class TestHaltInputValidation:
    async def test_empty_reason_rejected(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_halt(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "halt")
        interaction = _build_mock_interaction()

        await callback(interaction, "   ")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "required" in msg.lower()
        # Must NOT have called the api
        stub_client.invoke_kill_switch.assert_not_called()  # type: ignore[attr-defined]

    async def test_too_long_reason_rejected(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_halt(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "halt")
        interaction = _build_mock_interaction()

        await callback(interaction, "x" * 250)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "too long" in msg.lower()
        assert "250" in msg
        stub_client.invoke_kill_switch.assert_not_called()  # type: ignore[attr-defined]

    async def test_happy_path_sends_confirm_view(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_halt(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "halt")
        interaction = _build_mock_interaction()

        await callback(interaction, "regime change suspected")

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        embed = kwargs["embed"]
        assert "regime change suspected" in (embed.description or "") + (
            embed.title or ""
        ) + " ".join((f.value or "") for f in embed.fields)
        view = kwargs["view"]
        assert isinstance(view, HaltConfirmView)


# ---------------------------------------------------------------------------
# HaltConfirmView buttons
# ---------------------------------------------------------------------------


def _find_button(view: discord.ui.View, custom_id: str) -> discord.ui.Button[Any]:
    """Pull the Button item from view.children by custom_id.

    discord.py wraps the user's button method in a _ViewCallback that
    auto-binds (view, item) on invocation. Calling ``button.callback(
    interaction)`` is the production-equivalent invocation path.
    """
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id == custom_id:
            return child
    raise LookupError(f"button {custom_id!r} not found in view.children")


class TestHaltConfirmView:
    async def test_confirm_button_invokes_api_success(self, stub_client: ApiClient) -> None:
        stub_client.invoke_kill_switch.return_value = KillSwitchInvokeResponse(  # type: ignore[attr-defined]
            audit_event_uuid="aud-1",
            new_state="HALT_NEW",
            severity="routine",
        )
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "halt_confirm")

        await confirm_button.callback(interaction)

        stub_client.invoke_kill_switch.assert_awaited_once_with(  # type: ignore[attr-defined]
            trigger="manual_judgment",
            reason="why",
        )
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        # Success embed renders new_state + audit_event_uuid
        assert "HALT_NEW" in (embed.title or "")
        assert "aud-1" in (embed.description or "")
        # Both buttons disabled after the confirm fires
        assert confirm_button.disabled is True

    async def test_confirm_button_renders_501_special_case(self, stub_client: ApiClient) -> None:
        stub_client.invoke_kill_switch.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=501,
            error_code="KILL_SWITCH_HANDLER_NOT_WIRED",
            message="stub",
        )
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "halt_confirm")

        await confirm_button.callback(interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        body = embed.description or ""
        assert "Week 4 Wed dispatcher" in body

    async def test_cancel_button_edits_message(self, stub_client: ApiClient) -> None:
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        cancel_button = _find_button(view, "halt_cancel")

        await cancel_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "cancelled" in (embed.title or "").lower()
        # No api call was made
        stub_client.invoke_kill_switch.assert_not_called()  # type: ignore[attr-defined]

    async def test_interaction_check_rejects_non_invoker(self, stub_client: ApiClient) -> None:
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        # Different user clicks the button
        intruder = _build_mock_interaction(user_id=999)

        allowed = await view.interaction_check(intruder)

        assert allowed is False
        intruder.response.send_message.assert_awaited_once()
        msg = intruder.response.send_message.await_args.args[0]
        assert "operator" in msg.lower()

    async def test_interaction_check_rejects_after_consume(self, stub_client: ApiClient) -> None:
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        view._consumed = True  # simulate a prior click
        interaction = _build_mock_interaction(user_id=100)

        allowed = await view.interaction_check(interaction)

        assert allowed is False
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "already been used" in msg.lower()

    async def test_interaction_check_passes_for_invoker_pristine(
        self, stub_client: ApiClient
    ) -> None:
        view = HaltConfirmView(
            api_client=stub_client,
            invoker_id=100,
            reason="why",
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)

        allowed = await view.interaction_check(interaction)

        assert allowed is True


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


class TestStatusCommand:
    async def test_happy_path_defers_then_followup(self, stub_client: ApiClient) -> None:
        stub_client.get_health.return_value = _ok_health_response()  # type: ignore[attr-defined]
        tree = _build_tree()
        register_status(tree, api_client=stub_client)
        callback = _command_callback(tree, "status")
        interaction = _build_mock_interaction()

        await callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        stub_client.get_health.assert_awaited_once()  # type: ignore[attr-defined]
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "Status" in (embed.title or "")
        assert "paper" in (embed.title or "")

    async def test_api_error_renders_error_embed(self, stub_client: ApiClient) -> None:
        stub_client.get_health.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=500,
            error_code="INTERNAL_ERROR",
            message="boom",
        )
        tree = _build_tree()
        register_status(tree, api_client=stub_client)
        callback = _command_callback(tree, "status")
        interaction = _build_mock_interaction()

        await callback(interaction)

        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "INTERNAL_ERROR" in (embed.title or "")
        assert "boom" in (embed.description or "")


# ---------------------------------------------------------------------------
# /approve input validation + happy path → confirm view
# ---------------------------------------------------------------------------


_VALID_UUID = "12345678-1234-5678-1234-567812345678"


class TestApproveInputValidation:
    async def test_invalid_uuid_returns_ephemeral_error(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_approve(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "approve")
        interaction = _build_mock_interaction()

        await callback(interaction, "not-a-uuid")

        interaction.response.send_message.assert_awaited_once()
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert "Invalid signal_id" in (embed.title or "")
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        # Must NOT have called the api
        stub_client.approve_signal.assert_not_called()  # type: ignore[attr-defined]

    async def test_empty_uuid_rejected(self, stub_client: ApiClient) -> None:
        # Whitespace-only stripped → empty string → not a UUID.
        tree = _build_tree()
        register_approve(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "approve")
        interaction = _build_mock_interaction()

        await callback(interaction, "   ")

        interaction.response.send_message.assert_awaited_once()
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert "Invalid signal_id" in (embed.title or "")
        stub_client.approve_signal.assert_not_called()  # type: ignore[attr-defined]

    async def test_truncates_long_garbage_in_title(self, stub_client: ApiClient) -> None:
        # Pasting a paragraph as signal_id shouldn't crash with
        # embed-title-too-long. Title is capped at 100 chars.
        tree = _build_tree()
        register_approve(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "approve")
        interaction = _build_mock_interaction()

        long_garbage = "x" * 500
        await callback(interaction, long_garbage)

        embed = interaction.response.send_message.await_args.kwargs["embed"]
        # Discord caps title at 256 — render should be safely under that.
        assert len(embed.title or "") < 200

    async def test_happy_path_sends_confirm_view(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_approve(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "approve")
        interaction = _build_mock_interaction()

        await callback(interaction, _VALID_UUID)

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        embed = kwargs["embed"]
        # Confirm body mentions the signal_id verbatim.
        full_text = (embed.description or "") + " ".join((f.value or "") for f in embed.fields)
        assert _VALID_UUID in full_text
        view = kwargs["view"]
        assert isinstance(view, ApproveConfirmView)
        # Must NOT have called the api yet — only after the operator clicks ✓.
        stub_client.approve_signal.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ApproveConfirmView buttons
# ---------------------------------------------------------------------------


class TestApproveConfirmView:
    async def test_confirm_button_invokes_api_success(self, stub_client: ApiClient) -> None:
        stub_client.approve_signal.return_value = SignalApproveResponse(  # type: ignore[attr-defined]
            signal_id=_VALID_UUID,
            new_status="approved",
            audit_event_uuid="aud-1",
            audit_sequence_no=42,
            intent_to_place_order=True,
        )
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "approve_confirm")

        await confirm_button.callback(interaction)

        stub_client.approve_signal.assert_awaited_once_with(_VALID_UUID)  # type: ignore[attr-defined]
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        # Success embed: title carries the first-8-chars of the signal id;
        # body carries the audit_event_uuid + audit_sequence_no.
        assert "Signal approved" in (embed.title or "")
        assert "aud-1" in (embed.description or "")
        assert "#42" in (embed.description or "")
        # Both buttons disabled after confirm fires.
        assert confirm_button.disabled is True

    async def test_confirm_button_renders_signal_not_found_special_case(
        self, stub_client: ApiClient
    ) -> None:
        stub_client.approve_signal.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=404,
            error_code="SIGNAL_NOT_FOUND",
            message="No such signal",
        )
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "approve_confirm")

        await confirm_button.callback(interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "Signal not found" in (embed.title or "")
        # Operator-actionable hint pointing at #signals embed footer.
        assert "#signals" in (embed.description or "")

    async def test_confirm_button_renders_signal_not_pending_special_case(
        self, stub_client: ApiClient
    ) -> None:
        stub_client.approve_signal.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=409,
            error_code="SIGNAL_NOT_PENDING",
            message="Signal already actioned",
        )
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "approve_confirm")

        await confirm_button.callback(interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "Signal not pending" in (embed.title or "")
        # Mentions the audit log path so the operator can confirm who acted.
        assert "audit log" in (embed.description or "").lower()

    async def test_confirm_button_renders_generic_error(self, stub_client: ApiClient) -> None:
        stub_client.approve_signal.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=503,
            error_code="SERVICE_UNAVAILABLE",
            message="db down",
        )
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "approve_confirm")

        await confirm_button.callback(interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        # Generic error path renders the canonical envelope.
        assert "SERVICE_UNAVAILABLE" in (embed.title or "")
        assert "db down" in (embed.description or "")

    async def test_cancel_button_edits_message(self, stub_client: ApiClient) -> None:
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        interaction = _build_mock_interaction(user_id=100)
        cancel_button = _find_button(view, "approve_cancel")

        await cancel_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        embed = kwargs["embed"]
        assert "cancel" in (embed.title or "").lower()
        # Must NOT have called the api.
        stub_client.approve_signal.assert_not_called()  # type: ignore[attr-defined]

    async def test_interaction_check_rejects_non_invoker(self, stub_client: ApiClient) -> None:
        # Single-operator deployment but defensive against guild-share.
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        intruder_interaction = _build_mock_interaction(user_id=999)

        result = await view.interaction_check(intruder_interaction)
        assert result is False
        intruder_interaction.response.send_message.assert_awaited_once()
        msg = intruder_interaction.response.send_message.await_args.args[0]
        assert "operator who invoked" in msg

    async def test_interaction_check_rejects_after_consume(self, stub_client: ApiClient) -> None:
        view = ApproveConfirmView(
            api_client=stub_client,
            invoker_id=100,
            signal_id=_VALID_UUID,
            environment="paper",
        )
        # Manually flip the consumed flag (simulates a fast double-click
        # where the second interaction lands while the first is in-flight).
        view._consumed = True

        interaction = _build_mock_interaction(user_id=100)
        result = await view.interaction_check(interaction)
        assert result is False
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "already been used" in msg


# ---------------------------------------------------------------------------
# Registration sanity (smoke that all 4 register fns run without error)
# ---------------------------------------------------------------------------


class TestRegistrationSanity:
    def test_all_commands_register_without_guild(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        register_halt(tree, api_client=stub_client, environment="paper")
        register_status(tree, api_client=stub_client)
        register_approve(tree, api_client=stub_client, environment="paper")
        names = {cmd.name for cmd in tree.get_commands()}
        assert {"positions", "halt", "status", "approve"}.issubset(names)

    def test_all_commands_register_with_guild(self, stub_client: ApiClient) -> None:
        guild = discord.Object(id=12345)
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper", guild=guild)
        register_halt(tree, api_client=stub_client, environment="paper", guild=guild)
        register_status(tree, api_client=stub_client, guild=guild)
        register_approve(tree, api_client=stub_client, environment="paper", guild=guild)
        # Guild-scoped commands aren't returned by get_commands() with no guild arg
        names = {cmd.name for cmd in tree.get_commands(guild=guild)}
        assert {"positions", "halt", "status", "approve"}.issubset(names)


# ---------------------------------------------------------------------------
# Discord constraints — slash command descriptions ≤100 chars
# ---------------------------------------------------------------------------


class TestSlashCommandDescriptionLength:
    """Discord caps slash-command descriptions at 100 chars; over that
    triggers ``CommandSyncFailure (HTTP 400, error code 50035)`` at
    ``tree.sync()`` time — silently passes Python lint + unit tests until
    the bot tries to register against the gateway in production.

    Locks the contract for ALL registered commands by walking the tree
    after every register() runs. Future commands automatically get the
    coverage; no per-command opt-in required.

    Discord constants per developer docs (locked here so a future API
    bump that loosens the limit is a deliberate edit, not a silent
    drift):
      * Command name: 1-32 chars
      * Command description: 1-100 chars
    """

    DISCORD_COMMAND_DESCRIPTION_MAX = 100

    def test_all_command_descriptions_within_discord_limit(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        register_halt(tree, api_client=stub_client, environment="paper")
        register_status(tree, api_client=stub_client)
        register_approve(tree, api_client=stub_client, environment="paper")
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")

        too_long: list[tuple[str, int]] = []
        for cmd in tree.get_commands():
            desc = cmd.description or ""
            if len(desc) > self.DISCORD_COMMAND_DESCRIPTION_MAX:
                too_long.append((cmd.name, len(desc)))

        assert too_long == [], (
            f"Discord caps slash-command descriptions at "
            f"{self.DISCORD_COMMAND_DESCRIPTION_MAX} chars; the following "
            f"exceed: {too_long}. Shorten the description in the matching "
            f"register_* function. (Caught at gateway-sync as "
            f"CommandSyncFailure HTTP 400 error code 50035 in production.)"
        )

    def test_all_command_names_within_discord_limit(self, stub_client: ApiClient) -> None:
        # Discord caps slash-command name at 32 chars (lowercase, hyphens
        # OK). Phase-0 names are 5-9 chars; locking the bound prevents a
        # future verbose name from regressing.
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        register_halt(tree, api_client=stub_client, environment="paper")
        register_status(tree, api_client=stub_client)
        register_approve(tree, api_client=stub_client, environment="paper")
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")

        for cmd in tree.get_commands():
            assert 1 <= len(cmd.name) <= 32, (
                f"Discord requires command name 1-32 chars; got {cmd.name!r} (len {len(cmd.name)})"
            )


# ---------------------------------------------------------------------------
# /capital-deposit + /capital-withdraw
# ---------------------------------------------------------------------------


class TestCapitalEventAmountValidation:
    """Cover ``_validate_amount`` defensive branches + cosmetic Decimal
    rendering. Discord's app_commands API constrains the slash-command
    input to `float` client-side, but the bot defensively rejects
    non-positive / NaN / Infinity + applies USD-cent rendering rules."""

    def test_whole_dollar_strips_trailing_cents(self) -> None:
        s, err = _validate_amount(25000.0)
        assert err is None
        assert s == "25000"

    def test_fractional_preserves_cents(self) -> None:
        s, err = _validate_amount(25000.5)
        assert err is None
        assert s == "25000.50"

    def test_two_decimal_amount_preserved(self) -> None:
        s, err = _validate_amount(25000.12)
        assert err is None
        assert s == "25000.12"

    def test_quantize_rounds_to_2_decimals(self) -> None:
        s, err = _validate_amount(25000.125)
        assert err is None
        # 25000.125 rounds via ROUND_HALF_EVEN → 25000.12 (banker's rounding;
        # the 5 is exactly half + the preceding digit 2 is even so it stays).
        # The test asserts the resulting string is 2-decimal-formatted.
        assert s in {"25000.12", "25000.13"}
        assert "." in s

    def test_zero_amount_rejected(self) -> None:
        s, err = _validate_amount(0.0)
        assert s is None
        assert err is not None and "positive" in err

    def test_negative_amount_rejected(self) -> None:
        s, err = _validate_amount(-100.0)
        assert s is None
        assert err is not None and "positive" in err

    def test_nan_rejected(self) -> None:
        s, err = _validate_amount(float("nan"))
        assert s is None
        assert err is not None
        assert "NaN" in err or "valid number" in err

    def test_infinity_rejected(self) -> None:
        s, err = _validate_amount(float("inf"))
        assert s is None
        assert err is not None
        assert "Infinity" in err or "valid number" in err


class TestCapitalEventReasonValidation:
    """Cover ``_validate_reason`` length + whitespace defenses."""

    def test_happy_path_strips_whitespace(self) -> None:
        s, err = _validate_reason("  initial live funding  ")
        assert err is None
        assert s == "initial live funding"

    def test_empty_rejected(self) -> None:
        s, err = _validate_reason("   ")
        assert s is None
        assert err is not None and "required" in err

    def test_too_long_rejected(self) -> None:
        long = "x" * 501
        s, err = _validate_reason(long)
        assert s is None
        assert err is not None and "too long" in err

    def test_exact_max_accepted(self) -> None:
        exact = "x" * 500
        s, err = _validate_reason(exact)
        assert err is None
        assert s == exact


class TestCapitalEventDepositCommand:
    """Slash-command surface — `/capital-deposit <amount> <reason>` defers
    to a confirmation view; api call only fires on the confirm button."""

    async def test_happy_path_opens_confirm_view(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "capital-deposit")
        interaction = _build_mock_interaction()

        await callback(interaction, 25000.0, "initial live funding")

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        view = kwargs.get("view")
        assert isinstance(view, CapitalEventConfirmView)
        assert view._event_type == "deposit"
        assert view._amount_usd == "25000"
        # Reason whitespace stripped by _validate_reason
        assert view._reason == "initial live funding"
        # No api call yet — view is awaiting confirm click
        stub_client.invoke_capital_event.assert_not_called()  # type: ignore[attr-defined]

    async def test_zero_amount_rejected_ephemerally(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "capital-deposit")
        interaction = _build_mock_interaction()

        await callback(interaction, 0.0, "x")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "positive" in msg.lower()

    async def test_empty_reason_rejected_ephemerally(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "capital-deposit")
        interaction = _build_mock_interaction()

        await callback(interaction, 100.0, "   ")

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "reason" in msg.lower()


class TestCapitalEventWithdrawCommand:
    """Mirrors `/capital-deposit` shape; event_type=withdrawal."""

    async def test_happy_path_opens_withdrawal_confirm_view(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_capital_event_commands(tree, api_client=stub_client, environment="paper")
        callback = _command_callback(tree, "capital-withdraw")
        interaction = _build_mock_interaction()

        await callback(interaction, 5000.5, "quarterly distribution")

        interaction.response.send_message.assert_awaited_once()
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, CapitalEventConfirmView)
        assert view._event_type == "withdrawal"
        assert view._amount_usd == "5000.50"


class TestCapitalEventConfirmView:
    """State machine: invoker-only, single-use, confirm fires api call,
    cancel edits message. Mirrors ``TestHaltConfirmView``."""

    def _view(
        self, stub_client: ApiClient, *, event_type: str = "deposit"
    ) -> CapitalEventConfirmView:
        return CapitalEventConfirmView(
            api_client=stub_client,
            invoker_id=100,
            event_type=event_type,  # type: ignore[arg-type]
            amount_usd="25000",
            reason="test",
            environment="paper",
        )

    async def test_confirm_threshold_met_renders_success_embed_green(
        self, stub_client: ApiClient
    ) -> None:
        stub_client.invoke_capital_event.return_value = CapitalEventInvokeResponse(  # type: ignore[attr-defined]
            capital_event_id="ce-1",
            event_type="deposit",
            amount_usd="25000",
            threshold_met=True,
            post_event_equity="25000",
            capital_event_audit_event_uuid="aud-1",
            mode_started_audit_event_uuid="aud-2",
        )
        view = self._view(stub_client)
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "capital_event_confirm")

        await confirm_button.callback(interaction)

        stub_client.invoke_capital_event.assert_awaited_once_with(  # type: ignore[attr-defined]
            event_type="deposit",
            amount_usd="25000",
            reason="test",
        )
        interaction.followup.send.assert_awaited_once()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        # Title includes the amount; description includes the audit UUIDs
        assert "deposit" in (embed.title or "").lower()
        assert "aud-1" in (embed.description or "")
        # Threshold-met success uses the OK-green color (not INFO-blue).
        # discord.Embed normalizes int colors to discord.Color; compare values.
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_OK
        # mode_started UUID rendered as a separate field
        field_names = [f.name for f in embed.fields]
        assert "Mode started audit" in field_names
        # Both buttons disabled
        assert confirm_button.disabled is True

    async def test_confirm_threshold_not_met_omits_mode_field(self, stub_client: ApiClient) -> None:
        stub_client.invoke_capital_event.return_value = CapitalEventInvokeResponse(  # type: ignore[attr-defined]
            capital_event_id="ce-2",
            event_type="deposit",
            amount_usd="100",
            threshold_met=False,
            post_event_equity="25100",
            capital_event_audit_event_uuid="aud-3",
            mode_started_audit_event_uuid=None,
        )
        view = self._view(stub_client)
        view._amount_usd = "100"
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "capital_event_confirm")

        await confirm_button.callback(interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        # No mode_started_audit_event_uuid → no "Mode started audit" field
        field_names = [f.name for f in embed.fields]
        assert "Mode started audit" not in field_names
        # Threshold-NOT-met uses INFO-blue (event recorded but no mode activation)
        assert embed.color is not None
        assert embed.color.value == EMBED_COLOR_INFO

    async def test_confirm_api_error_renders_error_embed(self, stub_client: ApiClient) -> None:
        stub_client.invoke_capital_event.side_effect = ApiClientHTTPError(  # type: ignore[attr-defined]
            status_code=422,
            error_code="WITHDRAWAL_EXCEEDS_EQUITY",
            message="withdrawal amount_usd=30000 exceeds pre_event_equity=25000",
        )
        view = self._view(stub_client, event_type="withdrawal")
        interaction = _build_mock_interaction(user_id=100)
        confirm_button = _find_button(view, "capital_event_confirm")

        await confirm_button.callback(interaction)

        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert "failed" in (embed.title or "").lower()
        assert "WITHDRAWAL_EXCEEDS_EQUITY" in (embed.description or "")

    async def test_cancel_button_edits_message_no_api_call(self, stub_client: ApiClient) -> None:
        view = self._view(stub_client)
        interaction = _build_mock_interaction(user_id=100)
        cancel_button = _find_button(view, "capital_event_cancel")

        await cancel_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "cancelled" in (embed.title or "").lower()
        stub_client.invoke_capital_event.assert_not_called()  # type: ignore[attr-defined]

    async def test_interaction_check_rejects_non_invoker(self, stub_client: ApiClient) -> None:
        view = self._view(stub_client)
        intruder = _build_mock_interaction(user_id=999)

        allowed = await view.interaction_check(intruder)

        assert allowed is False
        intruder.response.send_message.assert_awaited_once()
        msg = intruder.response.send_message.await_args.args[0]
        assert "operator" in msg.lower()

    async def test_interaction_check_rejects_after_consume(self, stub_client: ApiClient) -> None:
        view = self._view(stub_client)
        view._consumed = True
        interaction = _build_mock_interaction(user_id=100)

        allowed = await view.interaction_check(interaction)

        assert allowed is False
        msg = interaction.response.send_message.await_args.args[0]
        assert "already been used" in msg.lower()
