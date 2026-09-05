"""
PorxPy — Portfolio X-ray Python.

Single source of truth for program name, version, and build date. Every
other module that needs these values imports them from here, and the
running Flask app exposes them via /api/meta so the frontend can show
them in the page header.

Version policy (informal semver):
    0.x.0  bumped on significant refactors / new features
    0.0.x  bumped on small bugfixes / adjustments
    1.0.0  reserved for first "stable" release

A batch that mixes the two takes the minor bump — the feature is what
someone reading the history is looking for, and a fix shipped alongside
it is described in the changelog entry either way.

Bumped once per batch of work handed over, not once per working session:
the number is how anyone tells which build they are running (it is in the
startup banner, in /api/meta and in the page header), so a second round
of changes reusing the previous round's number leaves that question
unanswerable. Every bump carries a matching CHANGELOG.md entry.
"""

NAME       = "PorxPy"
VERSION    = "0.103.1"
BUILD_DATE = "2026-09-05"

__all__ = ["NAME", "VERSION", "BUILD_DATE"]
