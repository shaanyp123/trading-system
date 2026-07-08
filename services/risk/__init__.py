"""services/risk package — risk engine.

Re-exports the canonical sizing pipeline + types so consumers can import from
``services.risk`` instead of reaching into submodules. (Crypto-pivot C0-B4:
the V1-era ``ContractMetadata``/``PendingAuditEvent`` exports died with the
CME Stages 0-5 pipeline; the crypto pipeline's types replace them.)
"""

from services.risk.dispatch import (
    AppliedStateTransition,
    apply_state_transition,
)
from services.risk.sizing import (
    CryptoProductSpec,
    SizingError,
    SizingInputs,
    SizingResult,
    run_sizing_pipeline,
)

__all__ = [
    "AppliedStateTransition",
    "CryptoProductSpec",
    "SizingError",
    "SizingInputs",
    "SizingResult",
    "apply_state_transition",
    "run_sizing_pipeline",
]
