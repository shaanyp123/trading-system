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
    HealthCheck,
    HealthResponse,
    KillSwitchInvokeResponse,
    PositionsResponse,
)
from services.discord_bot.commands.halt import HaltConfirmView, register_halt
from services.discord_bot.commands.positions import register_positions
from services.discord_bot.commands.status import register_status

# ---------------------------------------------------------------------------
# Fixtures: stub ApiClient + mock Interaction
# ---------------------------------------------------------------------------


def _stub_api_client() -> ApiClient:
    """Construct a real ApiClient instance and replace its async methods.

    The bot code uses ``ApiClient`` as a typed contract; we don't subclass
    or mock the entire class, just replace the three async methods with
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
# Registration sanity (smoke that all 3 register fns run without error)
# ---------------------------------------------------------------------------


class TestRegistrationSanity:
    def test_all_three_commands_register_without_guild(self, stub_client: ApiClient) -> None:
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper")
        register_halt(tree, api_client=stub_client, environment="paper")
        register_status(tree, api_client=stub_client)
        names = {cmd.name for cmd in tree.get_commands()}
        assert {"positions", "halt", "status"}.issubset(names)

    def test_all_three_commands_register_with_guild(self, stub_client: ApiClient) -> None:
        guild = discord.Object(id=12345)
        tree = _build_tree()
        register_positions(tree, api_client=stub_client, environment="paper", guild=guild)
        register_halt(tree, api_client=stub_client, environment="paper", guild=guild)
        register_status(tree, api_client=stub_client, guild=guild)
        # Guild-scoped commands aren't returned by get_commands() with no guild arg
        names = {cmd.name for cmd in tree.get_commands(guild=guild)}
        assert {"positions", "halt", "status"}.issubset(names)
