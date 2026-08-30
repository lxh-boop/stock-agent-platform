from .settings import (
    ACTIVE_MODEL_BACKEND,
    ACTIVE_MODEL_NAME,
    ACTIVE_MODEL_VERSION,
    load_active_model_manifest,
    validate_active_model_assets,
)


def generate_active_ranking(*args, **kwargs):
    """Load the concrete engine only for the asynchronous ranking task."""

    from .service import generate_active_ranking as implementation

    return implementation(*args, **kwargs)

__all__ = [
    "ACTIVE_MODEL_BACKEND",
    "ACTIVE_MODEL_NAME",
    "ACTIVE_MODEL_VERSION",
    "generate_active_ranking",
    "load_active_model_manifest",
    "validate_active_model_assets",
]
