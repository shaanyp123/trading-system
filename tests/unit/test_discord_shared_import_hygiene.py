"""Import-graph hygiene locks for the split discord_bot / webhook_pusher images.

Regression guard for the CI failure class the unit suite cannot see
directly: the test venv installs EVERY dependency, so a module that
transitively imports sqlalchemy (or discord.py) passes pytest here and
then fails the docker-build import-sanity RUN of whichever slim image
lacks that dep. Concretely (PR #359): the bot's ``/cycle`` command
imported the digest builder from ``services.webhook_pusher.cycle_digest``,
whose package ``__init__`` eagerly imports the sqlalchemy-touching
dispatcher — ImportError in the discord_bot image, invisible locally.

Each probe runs in a CLEAN subprocess (this pytest process has already
imported everything, so in-process ``sys.modules`` checks would be
contaminated), imports one runtime's surface, and asserts the forbidden
modules never entered ``sys.modules``:

  * ``services.discord_shared.*`` — the dependency-neutral shared-builder
    package: stdlib only (no sqlalchemy / httpx / discord / pydantic, and
    neither heavy service package).
  * the discord_bot image surface (mirrors the Dockerfile import-sanity
    RUN) — no sqlalchemy / asyncpg, and NOTHING under
    ``services.webhook_pusher`` (its ``__init__`` pulls sqlalchemy).
  * the webhook_pusher image surface — no discord.py, and NOTHING under
    ``services.discord_bot`` (its ``__init__`` pulls discord.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE_TEMPLATE = """
import json
import sys

{import_block}

roots = sorted({{m.split(".")[0] for m in sys.modules}})
full = sorted(sys.modules)
print(json.dumps({{"roots": roots, "full": full}}))
"""


def _probe(import_block: str) -> tuple[set[str], list[str]]:
    """Run ``import_block`` in a clean interpreter; return (root modules, all modules)."""
    code = _PROBE_TEMPLATE.format(import_block=import_block)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe import failed (the docker-build sanity RUN would too):\n{result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return set(payload["roots"]), payload["full"]


def _modules_under(full: list[str], prefix: str) -> list[str]:
    return [m for m in full if m == prefix or m.startswith(prefix + ".")]


class TestDiscordSharedIsDependencyNeutral:
    def test_shared_builder_imports_stdlib_only(self) -> None:
        roots, full = _probe(
            "import services.discord_shared.cycle_digest\n"
            "import services.discord_shared.gates_digest\n"
            "import services.discord_shared.governance_digest"
        )
        forbidden_roots = {"sqlalchemy", "httpx", "discord", "pydantic", "asyncpg"}
        assert roots & forbidden_roots == set(), (
            f"services.discord_shared must stay dependency-neutral; "
            f"pulled {sorted(roots & forbidden_roots)}"
        )
        assert _modules_under(full, "services.webhook_pusher") == []
        assert _modules_under(full, "services.discord_bot") == []

    def test_shared_package_init_is_import_free(self) -> None:
        roots, full = _probe("import services.discord_shared")
        assert roots & {"sqlalchemy", "httpx", "discord", "pydantic"} == set()
        # nothing beyond the package itself
        assert _modules_under(full, "services.discord_shared") == ["services.discord_shared"]


class TestDiscordBotImageImportSurface:
    """Mirror of services/discord_bot/Dockerfile's import-sanity RUN.

    The bot venv ships discord.py + httpx + structlog + pydantic(+settings)
    + pyyaml ONLY — sqlalchemy/asyncpg are absent, so any transitive
    import of them (in particular via ``services.webhook_pusher.__init__``)
    breaks the image build.
    """

    IMPORT_BLOCK = (
        "from services.discord_bot.main import TradingBotClient\n"
        "from services.discord_bot.entrypoint import main as ep_main\n"
        "from services.discord_bot.commands import (\n"
        "    register_positions, register_halt, register_status, register_cycle,\n"
        ")\n"
        "from services.discord_bot.api_client import ApiClient\n"
        "from services.discord_bot.embeds import build_positions_embed\n"
    )

    def test_bot_surface_pulls_no_sqlalchemy_and_no_webhook_pusher(self) -> None:
        roots, full = _probe(self.IMPORT_BLOCK)
        assert roots & {"sqlalchemy", "asyncpg"} == set(), (
            "discord_bot import surface pulled sqlalchemy/asyncpg — the slim bot "
            "image lacks them; the docker-build job will fail"
        )
        offenders = _modules_under(full, "services.webhook_pusher")
        assert offenders == [], (
            f"discord_bot surface imported {offenders} — "
            "services.webhook_pusher.__init__ eagerly imports its "
            "sqlalchemy-touching dispatcher; share pure code via "
            "services.discord_shared instead"
        )


class TestWebhookPusherImageImportSurface:
    """Mirror of services/webhook_pusher/Dockerfile's import-sanity RUN.

    The reverse trap: the pusher image ships no discord.py, and
    ``services.discord_bot.__init__`` eagerly imports it.
    """

    IMPORT_BLOCK = (
        "import services.webhook_pusher.cli\n"
        "import services.webhook_pusher.dispatcher\n"
        "import services.webhook_pusher.payloads\n"
        "import services.webhook_pusher.sender\n"
        "import services.webhook_pusher.sse_subscriber\n"
        "import services.webhook_pusher.cycle_digest\n"
        "import services.webhook_pusher.cycle_digest_scheduler\n"
        "import services.webhook_pusher.governance_reports\n"
        "import services.webhook_pusher.__main__\n"
    )

    def test_pusher_surface_pulls_no_discord(self) -> None:
        roots, full = _probe(self.IMPORT_BLOCK)
        assert "discord" not in roots, (
            "webhook_pusher import surface pulled discord.py — the pusher image "
            "lacks it; the docker-build job will fail"
        )
        offenders = _modules_under(full, "services.discord_bot")
        assert offenders == [], (
            f"webhook_pusher surface imported {offenders} — "
            "services.discord_bot.__init__ eagerly imports discord.py; share "
            "pure code via services.discord_shared instead"
        )
