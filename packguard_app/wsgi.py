"""Production WSGI entry point.

Configure PACKGUARD_ENV and PACKGUARD_SECRET_KEY before starting the server.
"""

from app import app


__all__ = ["app"]