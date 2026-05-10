"""services/api/schemas — Pydantic v2 request/response models per backend-spec §4.1.

Each module mirrors a backend-spec §4.1.x subsection. Models use
``model_config = ConfigDict(extra="forbid")`` so unknown fields fail validation
loudly rather than being silently dropped — a regression guard that would have
caught the spec/code drift Day 5-6 hit on the `__Host-` cookie name.

Day 15 ships the response shapes for the IG §3 Week 5 Mon endpoint list:

  * §4.1.1 auth — ``AuthMeResponse``
  * §4.1.2 signals — ``SignalSummary`` + ``SignalListResponse`` + the three
    decision-action request bodies
  * §4.1.3 system & risk — ``SystemStatus`` + ``ReconciliationSummary`` +
    ``KillSwitchStatus`` + ``KillSwitchInvokeRequest`` +
    ``KillSwitchResumeRequest``
  * §4.1.5b today / positions / orders / fills / alerts / health-score
"""
