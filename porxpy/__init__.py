"""
PorxPy — Portfolio X-ray Python.

Single source of truth for program name, version, and build date. Every
other module that needs these values imports them from here, and the
running Flask app exposes them via /api/meta so the frontend can show
them in the page header.

Version policy (informal semver):
    0.x.0  bumped on significant refactors / new features
    0.1.x  bumped on small bugfixes / adjustments
    1.0.0  reserved for first "stable" release
"""

NAME       = "PorxPy"
VERSION    = "0.26.0"
BUILD_DATE = "2026-06-17"

__all__ = ["NAME", "VERSION", "BUILD_DATE"]
