"""services/risk package — risk engine.

Re-exports the canonical sizing pipeline + types so consumers can import from
``services.risk`` instead of reaching into submodules.
"""

from services.risk.dispatch import (
    AppliedStateTransition,
    apply_state_transition,
)
from services.risk.sizing import (
    ContractMetadata,
    PendingAuditEvent,
    SizingError,
    SizingInputs,
    SizingResult,
    run_sizing_pipeline,
)

__all__ = [
    "AppliedStateTransition",
    "ContractMetadata",
    "PendingAuditEvent",
    "SizingError",
    "SizingInputs",
    "SizingResult",
    "apply_state_transition",
    "run_sizing_pipeline",
]
