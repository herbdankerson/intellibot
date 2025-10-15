"""Top-level package for the agentic chatbot service."""

__all__ = ["create_app", "__version__"]
__version__ = "0.1.0"


def create_app():
    """Deferred import to avoid heavy dependencies at package import time."""

    from .main import create_app as _create_app

    return _create_app()
