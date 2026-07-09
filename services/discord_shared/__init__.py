"""services/discord_shared — dependency-neutral pure builders shared across
Discord-facing runtimes (discord_bot + webhook_pusher).

CONTRACT (locks the CI docker-build failure class this package exists to
fix): this ``__init__`` stays EMPTY of imports, and every module in here
imports ONLY stdlib (+ structlog if a module ever needs logging — both
images ship it). No httpx, no sqlalchemy, no discord.py, no pydantic, no
imports from ``services.discord_bot`` or ``services.webhook_pusher`` —
both of those package __init__s eagerly import their heavy runtime deps
(discord.py resp. sqlalchemy/httpx), and each container image
deliberately lacks the other's deps. A regression test
(``tests/unit/test_discord_shared_import_hygiene.py``) enforces this in
a clean subprocess.

§5-pattern note: shared pure builders live OUTSIDE dependency-heavy
packages.
"""
