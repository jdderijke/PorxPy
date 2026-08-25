"""Yahoo transport fixes — TLS impersonation pinning, and the trust store.

Two separate faults live here, because they surface as the *same* curl
error and are told apart only by which one makes it go away:

1. The impersonation profile (v0.72.1) — a handshake the middlebox
   cannot parse, reported as a certificate problem and not one.
2. The trust store (v0.79.1) — a middlebox whose root really is
   untrusted, because it is in the operating system's store and Python
   does not read that store.

Fault 1: the impersonation profile
----------------------------------
yfinance builds every session as ``curl_cffi.Session(impersonate="chrome")``
(``yfinance/_http.py``). ``"chrome"`` is an alias for the NEWEST Chrome
profile curl_cffi ships, and from Chrome 124 onward that profile enables
post-quantum key exchange (X25519Kyber768, later X25519MLKEM768). The
resulting ClientHello is large enough, and novel enough, that some
TLS-inspecting middleboxes — corporate proxies, several antivirus
products, some home-router "web protection" — fail the handshake. curl
reports the failure as::

    curl: (60) SSL certificate problem: unable to get local issuer
    certificate

which reads as a certificate problem and is not one. Measured on the
affected install, against Yahoo, with curl_cffi 0.15.0:

    chrome99 … chrome123, edge99, edge101, safari15_5   all succeed
    chrome124, chrome131, chrome133a                    all fail

Every profile at or below chrome123 completes the handshake; every
profile from chrome124 up fails. Pointing the session at a CA bundle
changes nothing *for this fault*, because the trust store was never its
problem — which is exactly how it is told apart from fault 2, where the
bundle is the whole fix and the profile is irrelevant.

What this does
--------------
Wraps ``yfinance._http.new_session`` so the impersonation profile is a
value we choose rather than "whatever is newest". Certificate
verification stays ON: this is not a ``verify=False`` workaround, and it
must never become one — disabling verification would hide a real
man-in-the-middle as readily as this benign one.

The profile is overridable with ``PORXPY_YF_IMPERSONATE`` for the case
where a future curl_cffi renames or retires the pinned one, or where a
different network needs a different profile.

Fault 2: the trust store
------------------------
An antivirus or corporate proxy that inspects HTTPS terminates the
connection itself and re-signs it with a root of its own. It installs
that root in the OPERATING SYSTEM's certificate store, which is why
every browser on the machine keeps working and shows no warning.

Python does not read that store. ``certifi`` ships a fixed list of
public CAs and nothing else, so libcurl — and ``requests``, and every
other Python client — sees a certificate chain ending in an issuer it
has never heard of, and says::

    curl: (60) SSL certificate problem: unable to get local issuer
    certificate

The same sentence as fault 1, from an entirely different cause. The
machine trusts this issuer; only Python does not. So the fix is to hand
libcurl a bundle of certifi's roots PLUS the machine's own, which is
what :func:`ca_bundle` builds. Verification stays ON, and no root is
trusted that the operating system did not already trust — this aligns
Python with the machine's trust policy rather than relaxing it.

Overridable with ``PORXPY_CA_BUNDLE`` (a path to a PEM file) for a
machine whose roots come from somewhere this cannot see.

Known limit: on Python 3.13+ this is not enough for the ``requests``
clients (OpenFIGI, justETF). 3.13 turned on ``VERIFY_X509_STRICT`` by
default, and some inspection roots — Avast's among them — are malformed
in a way that check rejects (basicConstraints not marked critical),
whatever bundle they are offered in. libcurl does not apply that check,
so Yahoo works. Relaxing it would be a global loosening of Python's
defaults for every connection the app makes, so it is deliberately NOT
done here; excluding the hosts from the scanner is the fix that keeps
the defaults intact.
"""

from __future__ import annotations

import os
import ssl
import tempfile

# Recent enough to look like a current browser to Yahoo, old enough to
# predate the post-quantum ClientHello. Not "chrome", deliberately: the
# alias moves with every curl_cffi release, so pinning to it would let
# an upgrade silently reintroduce the failure.
DEFAULT_IMPERSONATE = "chrome123"

# Tried in order if the pinned profile is not available in the installed
# curl_cffi. Ordered newest-first among the known-good set.
FALLBACK_IMPERSONATE = ("chrome123", "chrome120", "chrome116", "chrome110",
                        "chrome107", "chrome104", "chrome99")

_installed = False

# Built once per process and reused. Rebuilt on every start rather than
# cached on disk between runs, because the machine's roots can change
# under us — an antivirus update regenerates its root, and a bundle
# carrying the old one would fail exactly like no bundle at all.
_ca_bundle: str | None = None
_ca_bundle_built = False


def _os_root_pems() -> list[str]:
    """Every root certificate the operating system trusts, as PEM text.

    Windows only: ``ssl.enum_certificates`` does not exist elsewhere, and
    elsewhere it is not needed — OpenSSL on Linux and macOS already reads
    the system store, so there is nothing for this to add.

    Only the ROOT store, deliberately: that is the machine's set of trust
    ANCHORS. The CA store holds intermediates, and promoting an
    intermediate to an anchor would trust it for chains its own issuer
    never authorised.

    Returns:
        PEM strings, possibly empty. Never raises — a machine whose store
        cannot be read is one where certifi alone must do, which is what
        happens today.
    """
    enum = getattr(ssl, "enum_certificates", None)
    if enum is None:
        return []
    out: list[str] = []
    try:
        for der, _enc, _trust in enum("ROOT"):
            try:
                out.append(ssl.DER_cert_to_PEM_cert(der))
            except Exception:
                continue
    except Exception as exc:
        print(f"[YFSession] could not read the OS root store: {exc}")
    return out


def ca_bundle() -> str | None:
    """Path to a PEM holding certifi's roots plus the machine's own.

    Returns:
        The bundle's path, or ``None`` when there is nothing to add (no
        OS store to read, or certifi missing) — in which case callers
        should leave the default trust store alone rather than pass an
        empty file, which would trust nothing at all.
    """
    global _ca_bundle, _ca_bundle_built
    if _ca_bundle_built:
        return _ca_bundle
    _ca_bundle_built = True

    override = (os.environ.get("PORXPY_CA_BUNDLE") or "").strip()
    if override:
        if os.path.isfile(override):
            _ca_bundle = override
            print(f"[YFSession] CA bundle from PORXPY_CA_BUNDLE: {override}")
        else:
            print(f"[YFSession] PORXPY_CA_BUNDLE is not a file: {override}")
        return _ca_bundle

    try:
        import certifi
        base = open(certifi.where(), encoding="utf-8").read()
    except Exception as exc:
        print(f"[YFSession] certifi unavailable ({exc}); "
              f"leaving the default trust store alone")
        return None

    extra = _os_root_pems()
    if not extra:
        return None

    path = os.path.join(tempfile.gettempdir(), "porxpy-ca-bundle.pem")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([base] + extra))
        # Proof it is usable BEFORE anything is pointed at it: a bundle
        # that fails to parse would take out every connection the app
        # makes, which is worse than the problem it was built to solve.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=path)
    except Exception as exc:
        print(f"[YFSession] could not build a CA bundle ({exc}); "
              f"leaving the default trust store alone")
        return None

    _ca_bundle = path
    print(f"[YFSession] CA bundle: certifi + {len(extra)} OS root(s) "
          f"-> {path}")
    return _ca_bundle


def impersonate_target() -> str:
    """The profile to use, honouring the environment override."""
    return (os.environ.get("PORXPY_YF_IMPERSONATE") or "").strip() \
        or DEFAULT_IMPERSONATE


def install() -> str | None:
    """Patch yfinance's session factory. Idempotent.

    Returns:
        The profile now in force, or ``None`` when the patch could not
        be applied (yfinance missing, or not using the curl_cffi
        backend — in which case there is nothing to fix).
    """
    global _installed
    if _installed:
        return impersonate_target()
    try:
        from yfinance import _http as yf_http
    except Exception:
        return None
    if not getattr(yf_http, "HAS_CURL_CFFI", False):
        # The requests fallback does no impersonation, so it never hits
        # this failure. Nothing to patch.
        return None

    backend = getattr(yf_http, "_backend", None)
    if backend is None or not hasattr(backend, "Session"):
        return None

    wanted = impersonate_target()
    order = [wanted] + [f for f in FALLBACK_IMPERSONATE if f != wanted]
    # None when there is nothing to add, and then the kwarg is omitted
    # rather than passed as None — curl_cffi reads a falsy `verify` as
    # "do not verify", and silently turning verification off is the one
    # outcome this module must never produce.
    bundle = ca_bundle()
    extra_kw = {"verify": bundle} if bundle else {}

    def new_session():
        last_exc = None
        for prof in order:
            try:
                return backend.Session(impersonate=prof, **extra_kw)
            except Exception as exc:      # unknown profile in this build
                last_exc = exc
                continue
        # Every candidate rejected: let yfinance's own factory run, so
        # the failure surfaces as yfinance's rather than as ours.
        if last_exc is not None:
            print(f"[YFSession] no usable impersonation profile "
                  f"({last_exc}); falling back to yfinance's default")
        return backend.Session(impersonate="chrome", **extra_kw)

    # Rebind in EVERY yfinance module that imported the factory, not
    # just on _http. `data.py`, `base.py`, `multi.py` and
    # `scrapers/history.py` each do `from ._http import new_session`,
    # which copies the function object at import time — patching only
    # `_http.new_session` leaves all of them calling the original, and
    # the fix silently does nothing.
    import sys
    patched = 0
    yf_http.new_session = new_session
    for name, mod in list(sys.modules.items()):
        if not name.startswith("yfinance") or mod is None:
            continue
        if getattr(mod, "new_session", None) is not None and mod is not yf_http:
            setattr(mod, "new_session", new_session)
            patched += 1

    _installed = True
    print(f"[YFSession] TLS impersonation pinned to {wanted!r} "
          f"({patched + 1} binding(s); certificate verification stays enabled)")
    return wanted
