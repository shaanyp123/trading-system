"""scripts/operator_tools package — committed operator recovery + ad-hoc tooling.

Distinct from ``scripts/`` (which is for ceremony / seed / verification
work that runs from the operator's laptop): this subdir is for tools that
run inside production containers against production state to recover
from incidents. Anything here mutates audit-chain rows or other durable
state via the canonical service-side write paths (NEVER raw SQL); the
guardrails are documented per script.
"""
