"""
ISIN → Yahoo ticker resolution, plus per-holding ticker normalisation.

Two distinct concerns live here:

* :func:`build_ticker` (and helpers) — fund-level resolution, hitting
  OpenFIGI when needed. Multi-tier cached so we never hit OpenFIGI when
  we don't have to.

* :func:`clean_holding_ticker_input` + :func:`candidate_variants` —
  ticker normalisation for the symbols that appear inside a fund's
  holdings file. ``clean_holding_ticker_input`` does only minimal,
  unambiguous cleanup (uppercase, strip ``$``, strip ``" Equity"``);
  ``candidate_variants`` produces an ordered list of Yahoo-form
  candidates to try. The actual choice is made by Yahoo — see
  :func:`porxpy.extractors.get_symbol_info_cached`, which probes the
  candidates in order and returns the first one Yahoo recognises.
  This sidesteps the false-positive risk of proactive rewriting (e.g.
  AAPL ending in "PL" is NOT a Lisbon listing).

OpenFIGI's no-key tier rate-limits aggressively (a few requests per
minute). The persistent ISIN map (in ``porxpy.utils``) is what makes this
project usable without an API key — a successful lookup is cached for
:data:`porxpy.config.ISIN_MAP_TTL_DAYS` days.
"""

from __future__ import annotations

import re

import requests
import yfinance as yf

from porxpy.config import (
    MIC_TO_YF,
    OPENFIGI_URL,
)
from porxpy.utils import (
    isin_map_get,
    isin_map_put,
    load_isin_map,
    portfolio_ticker_hint,
)


# ---------------------------------------------------------------------------
# OpenFIGI primitives
# ---------------------------------------------------------------------------
def _figi_post(payload: list) -> list:
    """POST a batch request to OpenFIGI's mapping endpoint.

    Args:
        payload: A list of OpenFIGI request dicts (one per item).

    Returns:
        The parsed JSON response (a parallel list), or ``[]`` on any error.
    """
    try:
        r = requests.post(
            OPENFIGI_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=12,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"[OpenFIGI] {exc}")
        return []


def figi_listings_by_isin(isin: str, mic: str) -> list[dict]:
    """Query OpenFIGI for every instrument of an ISIN on one exchange.

    Uses the ``micCode`` request field with the ISO MIC — verified by
    the PorxPy OpenFIGI probe to be both accepted *and* to return more
    rows than ``exchCode``: every currency-variant line, not just the
    primary listing. ``exchCode`` (OpenFIGI's own code vocabulary) is
    deliberately not used here.

    Args:
        isin: ISIN code.
        mic: Exchange MIC (ISO, e.g. ``"XLON"``).

    Returns:
        The raw OpenFIGI ``data`` rows (each a dict with ``ticker``,
        ``name``, ``securityType`` etc.), or ``[]`` on no match / error.
        OpenFIGI never returns a ``currency`` or ``isin`` field — the
        caller must not rely on either.
    """
    isin = (isin or "").strip().upper()
    mic  = (mic or "").strip().upper()
    if not (isin and mic):
        return []
    result = _figi_post([{"idType": "ID_ISIN", "idValue": isin,
                          "micCode": mic}])
    if not result:
        return []
    data = result[0].get("data") if isinstance(result[0], dict) else None
    return data or []


def classify_figi_tickers(rows: list[dict]) -> list[str]:
    """Pick out the *base* tickers from a set of OpenFIGI instrument rows.

    OpenFIGI returns, for one ISIN on one exchange, a mix of *base*
    tickers (``BATT``, ``BATG``) and *currency-suffixed* tickers
    (``BATTUSD``, ``BATTGBX``). Only base tickers correspond to real,
    Yahoo-recognised symbols; the suffixed forms are not Yahoo tickers
    and are discarded.

    The classification rule (agreed for PorxPy, robust against a base
    ticker that merely happens to end in three letters):

        A ticker is *suffixed* if another, shorter ticker in the same
        result set is a prefix of it. Otherwise it is a *base* ticker.

    So ``BATTUSD`` is suffixed because ``BATT`` is also present; ``BATT``
    and ``BATG`` are base because no shorter prefix of either appears.

    Args:
        rows: OpenFIGI ``data`` rows (from :func:`figi_listings_by_isin`).

    Returns:
        The distinct base tickers, order-preserved by first appearance.
    """
    tickers: list[str] = []
    seen: set[str] = set()
    for row in rows:
        tk = (row.get("ticker") or "").strip().upper()
        if tk and tk not in seen:
            seen.add(tk)
            tickers.append(tk)

    base: list[str] = []
    for tk in tickers:
        # tk is suffixed iff some strictly-shorter ticker in the set is
        # a prefix of it.
        is_suffixed = any(
            other != tk and len(other) < len(tk) and tk.startswith(other)
            for other in tickers
        )
        if not is_suffixed:
            base.append(tk)
    return base


# MIC → Yahoo suffix is MIC_TO_YF (config). The inverse — Yahoo suffix →
# MIC — is needed to turn a suffixed ticker ("BATT.L") into the MIC
# OpenFIGI wants ("XLON"). Built once at import. When several MICs share
# a suffix (e.g. .SW for XSWX and XVTX) the first MIC_TO_YF entry wins;
# that is fine because we only need *a* MIC OpenFIGI accepts for the
# exchange, and the primary MIC is the right pick.
YF_TO_MIC: dict[str, str] = {}
for _mic, _sfx in MIC_TO_YF.items():
    if _sfx and _sfx not in YF_TO_MIC:
        YF_TO_MIC[_sfx] = _mic
del _mic, _sfx


def split_yahoo_ticker(yf_symbol: str) -> tuple[str, str]:
    """Split a Yahoo ticker into ``(base, mic)``.

    ``"BATT.L"`` → ``("BATT", "XLON")``. A bare ticker with no Yahoo
    suffix (a US listing) → ``("BATT", "")`` — the empty MIC means "US
    listing", which OpenFIGI handles via the US exchange codes.

    Args:
        yf_symbol: A Yahoo-form ticker, with or without an exchange
            suffix.

    Returns:
        ``(base_ticker, mic)``. ``mic`` is ``""`` when the symbol has no
        recognised Yahoo suffix.
    """
    s = (yf_symbol or "").strip().upper()
    if "." in s:
        base, _, suf = s.rpartition(".")
        mic = YF_TO_MIC.get("." + suf, "")
        if mic:
            return base, mic
        # Unrecognised suffix — treat the whole thing as the base.
        return s, ""
    return s, ""


def resolve_mode1_listings(isin: str, mic: str) -> tuple[list[dict], str]:
    """Mode 1: resolve an ISIN + exchange to currency-tagged Yahoo tickers.

    The mode-1 fetch flow. The user gave an ISIN and an exchange; this:

    1. Queries OpenFIGI (``micCode``) for every instrument of the ISIN
       on that exchange.
    2. Keeps only the *base* tickers (:func:`classify_figi_tickers`) —
       the currency-suffixed forms are not real Yahoo tickers.
    3. Converts each base ticker to Yahoo form (base + the Yahoo suffix
       for the MIC) and probes Yahoo for it.
    4. Keeps only the ones Yahoo actually recognises, tagging each with
       the currency **Yahoo** reports. A base ticker that does not
       resolve on Yahoo is dropped silently — per PorxPy's agreed rule,
       only Yahoo-confirmed listings are ever offered.

    When several base tickers resolve, that is the genuine multi-currency
    case (e.g. the LSE GBp and USD lines of one ETF): the caller presents
    the returned currencies as a choice.

    Args:
        isin: ISIN code.
        mic: Exchange MIC (ISO).

    Returns:
        ``(listings, note)`` where ``listings`` is a list of
        ``{"yf_symbol", "currency"}`` dicts — one per Yahoo-confirmed
        base ticker — and ``note`` explains the outcome for the UI. The
        list is empty when nothing resolved.
    """
    isin = (isin or "").strip().upper()
    mic  = (mic or "").strip().upper()
    if not (isin and mic):
        return [], "ISIN and exchange are both required."

    rows = figi_listings_by_isin(isin, mic)
    if not rows:
        return [], (f"OpenFIGI has no listing for {isin} on {mic}.")

    base_tickers = classify_figi_tickers(rows)
    if not base_tickers:
        return [], (f"OpenFIGI returned only currency-variant tickers for "
                    f"{isin} on {mic}, no base ticker — cannot resolve.")

    suffix = MIC_TO_YF.get(mic, "")
    listings: list[dict] = []
    seen_syms: set[str] = set()
    dropped: list[str] = []
    for base in base_tickers:
        yf_symbol = base + suffix
        if yf_symbol in seen_syms:
            continue
        seen_syms.add(yf_symbol)
        currency = _probe_yf_currency(yf_symbol)
        if currency:
            listings.append({"yf_symbol": yf_symbol, "currency": currency})
        else:
            # Base ticker OpenFIGI knows but Yahoo does not — drop it.
            dropped.append(yf_symbol)

    if not listings:
        return [], (f"OpenFIGI found {len(base_tickers)} ticker(s) for "
                    f"{isin} on {mic}, but none resolved on Yahoo "
                    f"({', '.join(dropped)}).")

    curs = ", ".join(sorted({l['currency'] for l in listings}))
    note = (f"OpenFIGI+Yahoo: {isin} on {mic} → "
            f"{len(listings)} listing(s) [{curs}]")
    if dropped:
        note += f"; dropped (not on Yahoo): {', '.join(dropped)}"
    return listings, note


def validate_mode2_ticker(yf_symbol: str) -> tuple[str, str]:
    """Mode 2: validate a fully-qualified Yahoo ticker and read its currency.

    The mode-2 fetch flow. The user typed a complete Yahoo ticker —
    *with* its exchange suffix, exactly as it appears on the Yahoo
    Finance site (e.g. ``BATG.L``). There is no ISIN→ticker resolution
    here (no working data source exists for it); PorxPy simply confirms
    Yahoo recognises the exact symbol and returns live data for it.

    The ISIN is supplied separately by the user and is *not* resolved or
    validated here — in mode 2 the ISIN's only role is to be a unique,
    stable holdings-cache key.

    Args:
        yf_symbol: A fully-qualified Yahoo ticker, suffix included.

    Returns:
        ``(currency, note)``. ``currency`` is the ISO code Yahoo reports
        (empty string when the ticker did not resolve — i.e. validation
        failed); ``note`` explains the outcome.
    """
    yf_symbol = (yf_symbol or "").strip().upper()
    if not yf_symbol:
        return "", "No ticker given."
    currency = _probe_yf_currency(yf_symbol)
    if currency:
        return currency, f"Yahoo confirmed {yf_symbol} (currency {currency})."
    return "", (f"Yahoo did not return live data for {yf_symbol}. "
                f"Enter the full ticker including its exchange suffix, "
                f"exactly as on the Yahoo Finance website.")


def _probe_yf_currency(yf_symbol: str) -> str:
    """Best-effort lookup of a Yahoo symbol's trading currency.

    Used to annotate listing-picker rows so the user can tell two
    listings of the same fund apart (e.g. a GBP vs a USD line on the
    LSE). Never raises — an empty string just means "currency unknown",
    and the picker still shows the row.

    Args:
        yf_symbol: Yahoo-suffixed ticker.

    Returns:
        Uppercased ISO currency code, or ``""`` if it couldn't be found.
    """
    try:
        info = yf.Ticker(yf_symbol).info or {}
    except Exception as exc:
        print(f"[Listings] currency probe failed for {yf_symbol}: {exc}")
        return ""
    return (info.get("currency") or "").strip().upper()



# ---------------------------------------------------------------------------
# Top-level resolver
# ---------------------------------------------------------------------------
def build_ticker(isin: str, mic: str | None,
                 known_ticker: str | None = None
                 ) -> tuple[str, str, str]:
    """Resolve ``(ISIN, MIC)`` to a Yahoo ticker, avoiding OpenFIGI when possible.

    Resolution order, stopping at the first hit:

    1. ``known_ticker`` — explicit hint from the caller.
    2. Existing portfolio entries — any portfolio holding this ISIN with
       a matching MIC already has the ticker resolved.
    3. The persistent ``isin_map.json`` cache (TTL
       :data:`porxpy.config.ISIN_MAP_TTL_DAYS`).
    4. Live OpenFIGI lookup (and the result is then written back to the
       step-3 cache so we don't repeat it).

    As of the three-mode fetch flow, a ``mic`` is **required** for a
    live (tier-4) resolution — the old "no MIC → auto-pick the most
    liquid listing" behaviour has been removed. A call with no ``mic``
    that reaches tier 4 hard-fails (returns an empty ticker).

    ``build_ticker`` is kept for the ``known_ticker``/portfolio/cache
    fast paths used by :func:`porxpy.extractors.load_fund_data` and the
    portfolio-enrichment loop. The fetch route resolves identity itself
    (via :func:`resolve_mode1_listings` / :func:`validate_mode2_ticker`)
    and passes the result as ``known_ticker``, so tier 4 here is only a
    last-resort fallback for a portfolio entry that somehow lacks a
    cached ticker.

    Args:
        isin: ISIN code.
        mic: Exchange MIC. Required for a live OpenFIGI resolution.
        known_ticker: When the caller already knows the resolved ticker
            (e.g. from a portfolio entry), pass it here to bypass every
            other lookup tier.

    Returns:
        ``(yf_symbol, resolved_mic, note)``. ``yf_symbol`` is ``""`` when
        resolution failed (no MIC for a live lookup, or OpenFIGI found
        nothing). ``note`` describes the outcome; surfaced to the UI.
    """
    # 1. Explicit hint from caller
    if known_ticker:
        note = f"Using known ticker {known_ticker} (hint from caller)"
        # Deliberately not logged. This path is a pure return — no
        # network, no lookup — and it fires once per fund on every
        # portfolio load, cached or not. Logging it made a fully-cached
        # load look like it was resolving tickets over the wire, which
        # is misleading when diagnosing slow startups. The paths below
        # that DO cost something still log.
        return known_ticker, (mic or ""), note

    # 2. Any portfolio entry already has this resolved
    hint = portfolio_ticker_hint(isin, mic)
    if hint:
        tk, resolved_mic = hint
        note = f"Using {tk} from existing portfolio entry (MIC={resolved_mic or '?'})"
        print(f"[Resolve] {isin}/{mic or '?'} → {tk} [portfolio]")
        # Also write this to the isin_map so the Explore tab benefits on next run
        isin_map_put(isin, mic, tk, resolved_mic, note)
        return tk, resolved_mic, note

    # 3. Persistent ISIN map
    cached = isin_map_get(isin, mic)
    if cached and cached.get("ticker"):
        note = (f"Cached resolution: {isin}/{mic or '?'} → {cached['ticker']} "
                f"(resolved {cached.get('resolved_at','?')})")
        print(f"[Resolve] {isin}/{mic or '?'} → {cached['ticker']} [isin_map]")
        return cached["ticker"], cached.get("resolved_mic", mic or ""), note

    # 4. Live OpenFIGI — requires a MIC. Auto-select across exchanges
    #    has been removed: the fetch flow now always supplies one.
    if not mic:
        note = (f"No exchange given for {isin}. An exchange is required "
                f"to resolve a ticker — auto-selection has been removed.")
        print(f"[Resolve] {isin} → FAILED (no MIC)")
        return "", "", note

    # OpenFIGI gives us the base tickers for the ISIN on this exchange.
    # Without a currency to disambiguate (build_ticker has none), take
    # the first base ticker — this tier is only a fallback; the fetch
    # route's currency-aware path is resolve_mode1_listings.
    rows   = figi_listings_by_isin(isin, mic)
    base   = classify_figi_tickers(rows)
    suffix = MIC_TO_YF.get(mic.upper(), "")
    if base:
        sym  = base[0] + suffix
        note = f"OpenFIGI: {isin} on {mic} → {sym}"
        if len(base) > 1:
            note += f" (first of {len(base)} base tickers)"
        isin_map_put(isin, mic, sym, mic, note)
        return sym, mic, note
    # Do NOT cache failures — OpenFIGI may be rate-limited or briefly
    # unavailable; caching a miss for 30 days would be a footgun.
    note = f"OpenFIGI returned no result for {isin} on {mic}."
    print(f"[Resolve] {isin}/{mic} → FAILED (OpenFIGI miss)")
    return "", mic, note


# ---------------------------------------------------------------------------
# Per-holding ticker cleanup + variant generation (no network)
# ---------------------------------------------------------------------------
# Different fund issuers use different ticker conventions in their holdings
# files. Rather than rewrite the input proactively (which risks false
# positives — AAPL ending in "PL" looks like a Lisbon suffix), the
# resolver here just produces *candidate* forms; the per-symbol cache
# (porxpy.extractors.get_symbol_info_cached) tries them against Yahoo in
# order and keeps the first one that resolves. This way Yahoo itself is
# the gatekeeper for which candidate is right.
#
# Conventions handled:
#   * Plain Yahoo:        "PLTR", "VOD.L"               → as-is
#   * Bloomberg spaced:   "PLTR US", "VOD LN", "6758 JT" → "PLTR", "VOD.L", "6758.T"
#   * Bloomberg w/ asset: "PLTR US Equity"              → " Equity" trimmed in cleanup
#   * Concatenated:       "PLTRUS", "VODLN"             → "PLTR", "VOD.L"
#   * Refinitiv RIC:      "PLTR.OQ", "AAPL.O"           → "PLTR", "AAPL"
#                         ".L"/".PA"/".AS" stay (they ARE Yahoo suffixes)
#   * Other punctuation:  spaces, leading "$" stripped in cleanup

# Bloomberg country code → Yahoo suffix. Empty string means "no suffix"
# (Yahoo treats US listings as bare tickers).
_BLOOMBERG_TO_YF: dict[str, str] = {
    "US": "",      # NYSE/NASDAQ — Yahoo bare ticker
    "CN": "",      # Bloomberg "CN" = US OTC (e.g. PLTR CN OTC) - rare; bare
    "LN": ".L",    # London
    "GR": ".DE",   # XETRA
    "GY": ".DE",   # Frankfurt floor (Bloomberg uses GY for some)
    "FP": ".PA",   # Paris (Euronext)
    "NA": ".AS",   # Amsterdam (Euronext)
    "BB": ".BR",   # Brussels (Euronext)
    "PL": ".LS",   # Lisbon (Euronext)
    "IM": ".MI",   # Milan
    "SM": ".MC",   # Madrid (BME)
    "SE": ".ST",   # Stockholm
    "SS": ".ST",   # Bloomberg SS sometimes = Stockholm
    "SW": ".SW",   # SIX Swiss
    "NO": ".OL",   # Oslo
    "DC": ".CO",   # Copenhagen
    "FH": ".HE",   # Helsinki
    "AV": ".VI",   # Vienna
    "JT": ".T",    # Tokyo
    "JP": ".T",    # Tokyo (alt code)
    "HK": ".HK",   # Hong Kong
    "AU": ".AX",   # ASX
    "NZ": ".NZ",   # New Zealand
    "CT": ".TO",   # Toronto (Bloomberg "CN" is overloaded; CT also seen)
    "CA": ".TO",   # Canada — Bloomberg sometimes uses CA for Canada
    "SP": ".SI",   # Singapore
    "TT": ".TW",   # Taiwan
    "KS": ".KS",   # Korea (KOSPI)
    "KQ": ".KQ",   # KOSDAQ
    "IN": ".NS",   # India NSE
    "IS": ".BO",   # India BSE
    "BZ": ".SA",   # Brazil B3
    "MM": ".MX",   # Mexico
    "SJ": ".JO",   # Johannesburg
    "ID": ".JK",   # Jakarta (IDX)
    "MK": ".KL",   # Kuala Lumpur
    "TB": ".BK",   # Bangkok
}

# Refinitiv RIC suffixes that indicate US listings — strip them so we
# end up with the bare Yahoo ticker (Yahoo doesn't suffix US listings).
_REFINITIV_US_SUFFIXES = (".OQ", ".O", ".N", ".K", ".A", ".P", ".PK")

# Bloomberg-style trailers some files include verbatim after the country
# code (sometimes also without a country code). Trimmed during cleanup.
_BLOOMBERG_TRAILERS = (" EQUITY", " CORP", " INDEX", " COMDTY", " CURNCY")

# ISO 3166-1 alpha-2 country prefix of an ISIN → most-likely Yahoo suffix.
# Used as fallback when all variant candidates fail but we have an ISIN:
# strip any existing suffix from the ticker and try bare_ticker + this suffix.
# Only the most common European and Asia-Pacific markets are listed; US/CA
# ISINs (US / CA) map to bare tickers (no suffix). Omitted entries are
# silently skipped — we won't guess wildly.
# Country node -> Yahoo suffix, derived from _ISIN_PREFIX_TO_YF on
# first use by country_suffix_variant. None until then.
_COUNTRY_NODE_TO_YF: dict[str, str] | None = None

_ISIN_PREFIX_TO_YF: dict[str, str] = {
    "US": "",       # NYSE / NASDAQ — bare ticker
    "CA": ".TO",    # Toronto (most liquid)
    "GB": ".L",     # London
    "DE": ".DE",    # XETRA
    "FR": ".PA",    # Euronext Paris
    "NL": ".AS",    # Euronext Amsterdam
    "BE": ".BR",    # Euronext Brussels
    "PT": ".LS",    # Euronext Lisbon
    "IT": ".MI",    # Milan (Borsa Italiana)
    "ES": ".MC",    # Madrid BME
    "SE": ".ST",    # Stockholm
    "NO": ".OL",    # Oslo
    "DK": ".CO",    # Copenhagen
    "FI": ".HE",    # Helsinki
    "AT": ".VI",    # Vienna
    "CH": ".SW",    # SIX Swiss
    "JP": ".T",     # Tokyo
    "HK": ".HK",    # Hong Kong
    "AU": ".AX",    # ASX
    "NZ": ".NZ",    # New Zealand
    "SG": ".SI",    # Singapore
    "TW": ".TW",    # Taiwan
    "KR": ".KS",    # Korea KOSPI
    "IN": ".NS",    # India NSE
    "BR": ".SA",    # Brazil B3
    "MX": ".MX",    # Mexico
    "ZA": ".JO",    # Johannesburg
    "ID": ".JK",    # Jakarta
    "MY": ".KL",    # Kuala Lumpur
    "TH": ".BK",    # Bangkok
}


def clean_holding_ticker_input(raw: str | None) -> str:
    """First-pass cleanup of an issuer-supplied ticker string.

    Trims whitespace, uppercases, strips a leading ``$``, and drops a
    trailing Bloomberg asset-class qualifier (``" Equity"`` and friends).
    Also normalises dot-separated Bloomberg country codes to the spaced
    form (``"AIR.FP"`` → ``"AIR FP"``) so :func:`candidate_variants`
    can convert them to Yahoo suffixes via its existing Bloomberg-spaced
    path. This covers the common European issuer convention of writing
    ``"ASML.NA"`` or ``"MC.FP"`` instead of ``"ASML NA"`` or ``"MC FP"``.

    Does NOT attempt to rewrite Bloomberg/Refinitiv/concat forms beyond
    the above — those are handled by :func:`candidate_variants` and gated
    by Yahoo's response.

    Args:
        raw: Whatever the holdings file had in the ticker cell.

    Returns:
        The cleaned-up form, or ``""`` if input was empty.
    """
    if not raw:
        return ""
    s = str(raw).strip().upper()
    if not s:
        return ""
    if s.startswith("$"):
        s = s[1:].strip()
    for trailer in _BLOOMBERG_TRAILERS:
        if s.endswith(trailer):
            s = s[: -len(trailer)].strip()

    # Dot-separated Bloomberg country-code form: "TICKER.CC" where CC is a
    # known two-letter Bloomberg country code and the part before the dot is
    # at least one character. Rewrite to the spaced form "TICKER CC" so
    # candidate_variants' Bloomberg-spaced branch picks it up. We only
    # convert when the two chars after the last dot are a recognised Bloomberg
    # country code — this avoids misreading genuine Yahoo suffixes like
    # ".L" / ".PA" / ".AS" / ".DE" which already work as-is and have more
    # than two characters or don't match a Bloomberg code.
    if "." in s:
        dot_pos = s.rfind(".")
        after_dot = s[dot_pos + 1:]
        before_dot = s[:dot_pos]
        if (len(after_dot) == 2 and after_dot.isalpha()
                and before_dot
                and after_dot in _BLOOMBERG_TO_YF):
            s = before_dot + " " + after_dot

    return s


# Internal max number of candidates ``candidate_variants`` will return.
# The Yahoo lookup chokepoint also caps probes per symbol; we keep this
# slightly higher than that cap so the limiting happens consistently
# in one place.
_MAX_CANDIDATE_VARIANTS = 4


def candidate_variants(cleaned: str) -> list[str]:
    """Return ordered candidate Yahoo tickers for a cleaned input.

    The first element is always the cleaned input itself, so callers can
    do "try as-is, then variants" with a single iteration. The remaining
    elements are heuristic rewrites; duplicates and the as-is form are
    de-duplicated so each variant is tried at most once.

    Args:
        cleaned: Output of :func:`clean_holding_ticker_input`.

    Returns:
        Ordered list of distinct strings to try against Yahoo. Empty
        list iff ``cleaned`` is empty.
    """
    if not cleaned:
        return []
    out: list[str] = [cleaned]
    seen: set[str]  = {cleaned}

    def add(c: str) -> None:
        if c and c not in seen:
            out.append(c)
            seen.add(c)

    # 1. Refinitiv RIC: trailing ".OQ"/".O"/".N"/etc indicates a US listing.
    #    Strip to bare ticker. Skipped if the input doesn't end in one of
    #    these suffixes, or if stripping would leave an empty string.
    for suf in _REFINITIV_US_SUFFIXES:
        if cleaned.endswith(suf) and len(cleaned) > len(suf):
            add(cleaned[: -len(suf)])
            break   # only one Refinitiv suffix can apply

    # 2. Bloomberg spaced form: "PLTR US" / "VOD LN" / "6758 JT".
    #    Splits into [symbol, country] and looks up the Yahoo suffix.
    if " " in cleaned:
        parts = cleaned.split()
        if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
            symbol, country = parts
            if country in _BLOOMBERG_TO_YF:
                add(symbol + _BLOOMBERG_TO_YF[country])
        # Also try the collapsed form (every space removed) as a fallback —
        # some inputs have stray spaces that aren't the Bloomberg pattern.
        add("".join(parts))

    # 3. Concatenated form: "PLTRUS" / "VODLN" / "6758JT". With Yahoo as
    #    gatekeeper we don't need a length threshold — try the rewrite,
    #    Yahoo will reject "AAPL → AA.LS" (since AA.LS isn't a real symbol)
    #    and the as-is "AAPL" wins, which is correct. Only requires the
    #    last 2 chars are a known Bloomberg country code, and the input
    #    has no whitespace (a spaced form is a different convention,
    #    handled in step 2 above).
    if " " not in cleaned and len(cleaned) >= 4 and cleaned[-2:].isalpha():
        suffix_2 = cleaned[-2:]
        prefix   = cleaned[:-2]
        yf_sfx   = _BLOOMBERG_TO_YF.get(suffix_2)
        if yf_sfx is not None and prefix:
            add(prefix + yf_sfx)

    return out[:_MAX_CANDIDATE_VARIANTS]


def country_suffix_variant(cleaned: str, country: str | None) -> str | None:
    """Derive a Yahoo ticker from the COUNTRY the holdings file gave.

    The sibling of :func:`isin_country_variant`, for the very common row
    that carries a bare local ticker and no ISIN at all. An issuer's file
    says `SIE` / Duitsland / EUR, and Yahoo needs `SIE.DE`; the country
    column answers exactly the question the missing ISIN would have.

    The country is resolved through the geography tree first, so the
    issuer's own wording works — "Duitsland", "Germany" and "DE" are one
    answer. The suffix table is :data:`_ISIN_PREFIX_TO_YF`, reused rather
    than copied: there is one opinion in this codebase about which
    exchange a country's tickers most likely live on, and two would drift.

    Args:
        cleaned: Output of :func:`clean_holding_ticker_input`.
        country: The row's country, in any spelling the geography
            definitions recognise.

    Returns:
        A candidate Yahoo ticker, or ``None`` when the country does not
        resolve, has no suffix on file, or the candidate would repeat the
        input. Also ``None`` for countries whose suffix is the empty
        string (the US), since that candidate is the bare ticker the
        chain has already tried.
    """
    if not cleaned or not country:
        return None
    from porxpy.resources import resolve_country_tree

    node = (resolve_country_tree(country) or {}).get("country") or ""
    if not node or node == "unknown":
        return None
    # Country node -> alpha-2, by resolving each prefix in the suffix
    # table through the same tree. Built once, on first use.
    global _COUNTRY_NODE_TO_YF
    if _COUNTRY_NODE_TO_YF is None:
        built: dict[str, str] = {}
        for cc, suffix in _ISIN_PREFIX_TO_YF.items():
            n = (resolve_country_tree(cc) or {}).get("country") or ""
            if n and n != "unknown" and suffix:
                built.setdefault(n, suffix)
        _COUNTRY_NODE_TO_YF = built

    suffix = _COUNTRY_NODE_TO_YF.get(node)
    if not suffix:
        return None

    base = cleaned.split()[0] if " " in cleaned else cleaned.split(".")[0]
    if not base:
        return None
    candidate = base + suffix
    return None if candidate == cleaned else candidate


def isin_country_variant(cleaned: str, isin: str) -> str | None:
    """Derive a Yahoo ticker from an ISIN country prefix when all variants fail.

    Uses the first two characters of ``isin`` (the ISO 3166-1 alpha-2
    country code) to look up the most-likely Yahoo exchange suffix, then
    strips any existing suffix from ``cleaned`` and appends the ISIN-derived
    one.

    This handles the case where an issuer supplies a ticker in an unrecognised
    form (e.g. a local exchange code) together with an ISIN. Example:

    * ticker ``"AIR"`` (bare, no suffix), ISIN ``"FR0000120271"``
      → ``"AIR.PA"``
    * ticker ``"AIR FP"`` already handled by Bloomberg-spaced path, but
      ISIN fallback would also produce ``"AIR.PA"`` as a backstop.
    * ticker ``"RDSA NA"`` (already handled), ISIN ``"NL0000009132"``
      → backstop ``"RDSA.AS"``

    Args:
        cleaned: Output of :func:`clean_holding_ticker_input`. May include
            a Bloomberg-spaced country code (``"AIR FP"``) or be bare
            (``"AIR"``). The function always strips to the base symbol
            before appending the ISIN-derived suffix.
        isin: Full 12-character ISIN. Only the first two characters are used.

    Returns:
        A candidate Yahoo ticker string, or ``None`` if:
        * ``isin`` is shorter than 2 chars.
        * The country prefix is not in :data:`_ISIN_PREFIX_TO_YF`.
        * The derived candidate would be identical to ``cleaned`` (already
          tried).
        * ``cleaned`` is empty.
    """
    if not cleaned or not isin or len(isin) < 2:
        return None
    country_prefix = isin[:2].upper()
    if country_prefix not in _ISIN_PREFIX_TO_YF:
        return None
    yf_suffix = _ISIN_PREFIX_TO_YF[country_prefix]

    # Extract the base symbol — strip any existing space+country or suffix.
    base = cleaned
    if " " in base:
        # Bloomberg-spaced: "AIR FP" → "AIR"
        base = base.split()[0]
    elif "." in base:
        # Yahoo-suffixed: "AIR.PA" → "AIR"
        base = base.split(".")[0]

    if not base:
        return None

    candidate = base + yf_suffix
    # Don't return a candidate identical to the cleaned input (already tried).
    if candidate == cleaned:
        return None
    return candidate


def search_name_variant(name: str, ticker_prefix: str,
                        prefix_chars: int = 4) -> str | None:
    """Search Yahoo by security name and find a ticker matching the prefix.

    Used as the last-resort fallback when both variant probing and the ISIN
    country fallback have failed. Calls ``yfinance.Search`` with the
    security name and looks for a returned ticker whose first
    ``prefix_chars`` characters match those of ``ticker_prefix``.

    The prefix match is case-insensitive and uses only the base ticker
    (stripping any Yahoo suffix before comparing) so that ``"AIR"``
    matches both ``"AIR.PA"`` and ``"AIRBUS"`` but not ``"AIRG"``.

    Args:
        name: Security name from the holdings file (e.g. ``"Airbus SE"``).
        ticker_prefix: The cleaned raw ticker from the file (e.g. ``"AIR"``
            or ``"AIR FP"``). Only the first ``prefix_chars`` characters of
            the base ticker are used for matching.
        prefix_chars: How many leading characters of the base ticker to
            require as a match. Default 4 is conservative (reduces false
            positives for short tickers like ``"BP"``); callers may lower
            it when the ticker is genuinely short.

    Returns:
        The first Yahoo ticker from the search results whose base ticker
        starts with the required prefix, or ``None`` if the search fails,
        returns nothing, or no result matches.
    """
    if not name or not ticker_prefix:
        return None

    # Extract the base ticker prefix for matching (strip space / dot suffix).
    base_prefix = ticker_prefix.upper()
    if " " in base_prefix:
        base_prefix = base_prefix.split()[0]
    elif "." in base_prefix:
        base_prefix = base_prefix.split(".")[0]
    match_prefix = base_prefix[:prefix_chars]
    if not match_prefix:
        return None

    try:
        import yfinance as yf
        result = yf.Search(name, max_results=8)
        quotes = result.quotes or []
    except Exception as exc:
        print(f"[NameSearch] yf.Search('{name}') failed: {exc}")
        return None

    for q in quotes:
        sym = (q.get("symbol") or "").upper().strip()
        if not sym:
            continue
        # Compare the base ticker (strip suffix) to the required prefix.
        base_sym = sym.split(".")[0]
        if base_sym.startswith(match_prefix):
            return sym

    return None


# Corporate-form words that carry no identifying information. Stripped
# from both sides before two security names are compared, so "ASML
# Holding NV" and "ASML Holding N.V." are the same name and "Novo
# Nordisk A/S" matches "Novo Nordisk B A/S" on its first tokens.
_NAME_NOISE: frozenset[str] = frozenset({
    "nv", "bv", "sa", "sas", "se", "ag", "plc", "ltd", "limited", "inc",
    "incorporated", "corp", "corporation", "co", "company", "group",
    "holding", "holdings", "spa", "ab", "asa", "as", "oyj", "kgaa",
    "the", "class", "cl", "series", "ord", "ordinary", "shares", "share",
    "reg", "registered", "sponsored", "adr", "gdr", "a", "b", "c",
})


def _name_tokens(name: str) -> list[str]:
    """Comparable tokens of a security name: lowercase, no noise, no punctuation."""
    import re as _re
    words = _re.split(r"[^0-9a-zA-Z]+", (name or "").lower())
    # Single characters go too: they are share-class and legal-form
    # debris ("A/S" leaves an "s", "Class A" an "a") and matching on one
    # letter is not evidence of anything.
    return [w for w in words if len(w) > 1 and w not in _NAME_NOISE]


def search_name_only(name: str, max_results: int = 8) -> str | None:
    """Resolve a security by NAME alone, when there is no ticker to match on.

    Why this exists separately from :func:`search_name_variant`: that
    function verifies a search hit by comparing it to the ticker the file
    already supplied, and returns nothing when there is no ticker — which
    is exactly the case this one is for. A factsheet's position table
    routinely prints names and nothing else ("Bundesobligation 2,1%
    2032", "ASML Holding NV"), and until v0.77.0 such a row could not be
    enriched at all: the caller passed an empty prefix and the match
    could never succeed.

    With no ticker to check against, the NAME is the only evidence, so the
    match is deliberately strict — a wrong hit here would silently attach
    another company's sector, country and currency to the position, which
    is worse than leaving the row blank. A candidate is accepted only if
    its own name agrees with the query on every token the query has, in
    order, ignoring corporate-form words. "Novo Nordisk" matches "Novo
    Nordisk A/S"; it does not match "Novo Integrated Sciences".

    Args:
        name: The security name as printed.
        max_results: How many search hits to consider.

    Returns:
        A Yahoo ticker, or None when the search fails, returns nothing,
        or nothing agrees closely enough with the name.
    """
    want = _name_tokens(name)
    if not want:
        return None

    try:
        import yfinance as yf
        result = yf.Search(name, max_results=max_results)
        quotes = result.quotes or []
    except Exception as exc:
        print(f"[NameSearch] yf.Search('{name}') failed: {exc}")
        return None

    for q in quotes:
        sym = (q.get("symbol") or "").upper().strip()
        if not sym:
            continue
        cand = _name_tokens(f"{q.get('longname') or ''} {q.get('shortname') or ''}")
        if not cand:
            continue
        # Every query token must appear, in order, as a prefix of a
        # candidate token: "bundesobligation" matches, "bund" would too,
        # but a missing or reordered token rejects the hit.
        i = 0
        for w in want:
            while i < len(cand) and not cand[i].startswith(w):
                i += 1
            if i == len(cand):
                break
            i += 1
        else:
            # ASCII arrow deliberately: this line is new in a path that
            # now runs for every name-only holdings row, and a Windows
            # console in cp1252 raises on the "→" the older log lines use.
            print(f"[NameSearch] '{name}' -> {sym} "
                  f"({q.get('longname') or q.get('shortname') or ''})")
            return sym
    return None


# An ISIN is twelve characters: a two-letter ISO country code, nine
# alphanumerics, and a numeric check digit. One definition, here, because
# the shape was written out separately in app.py (deciding whether typed
# input is an ISIN or a ticker) and upload.py (deciding whether a cell
# holds one) — two copies of a format that is fixed by a standard, either
# of which could have been "fixed" without the other.
#
# ISIN_RE is the SHAPE alone, which is the right question for "does this
# typed input look like an ISIN rather than a ticker" — the two callers
# above want that and nothing more. `is_valid_isin` additionally verifies
# the check digit, which is the right question for "should I spend a
# network call on this, and should I trust it over the row's ticker".
#
# The check digit earns its keep now that a malformed identifier gets
# CORRECTED rather than merely skipped (v0.95.0): without it, an ISIN
# with a transposed digit is the right shape, fails to resolve, and is
# left on the row as though it were fine. With it, the row falls through
# to the ticker, resolves there, and the bad ISIN is replaced by the real
# one.
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# A CUSIP is nine characters: eight of issuer-and-issue, then a check
# digit. The rarely-used *, @ and # placeholders are not accepted — they
# appear in no holdings file this app has met, and admitting them would
# widen what counts as "valid enough to keep".
CUSIP_RE = re.compile(r"^[0-9A-Z]{8}[0-9]$")


def _luhn_ok(digits: str, check: int) -> bool:
    """Whether ``check`` is the Luhn check digit of ``digits``.

    Shared by both identifier checks below: ISIN and CUSIP use the same
    doubling rule over different alphabets, so the arithmetic lives once.
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10 == check


def is_valid_isin(value: str | None) -> bool:
    """Whether ``value`` is a well-formed ISIN, check digit included.

    Args:
        value: Any string, or None. Whitespace and case are forgiven —
            holdings files are full of " ie00b4l5y983 ".

    Returns:
        True when the trimmed, upper-cased value has the ISIN shape AND
        its final digit is the correct Luhn check over the first eleven
        characters, letters expanded to their two-digit values (A=10 …
        Z=35).
    """
    if not value:
        return False
    s = str(value).strip().upper()
    if not ISIN_RE.match(s):
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in s[:11])
    return _luhn_ok(digits, int(s[11]))


def is_valid_cusip(value: str | None) -> bool:
    """Whether ``value`` is a well-formed CUSIP, check digit included.

    Args:
        value: Any string, or None. Whitespace and case forgiven.

    Returns:
        True when the value has the CUSIP shape and its final digit is
        the correct check over the first eight characters (digits as
        themselves, letters as their position plus nine).
    """
    if not value:
        return False
    s = str(value).strip().upper()
    if not CUSIP_RE.match(s):
        return False
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in s[:8])
    return _luhn_ok(digits, int(s[8]))


def name_query_head(name: str, max_tokens: int = 4) -> str:
    """The searchable head of a security name.

    Bond holdings are named like "US TREASURY N/B 4.25% 15/11/2034" or
    "DEUTSCHLAND REP 0% 15/08/2031", where everything after the issuer is
    coupon and maturity — precise, and useless to a name search, which
    matches words. Feeding the whole string to Yahoo returns nothing;
    feeding the first few words returns the issuer.

    Stops at the first token that is not a word: a number, a percentage,
    a date fragment. So "US TREASURY N/B 4.25% ..." searches as
    "US TREASURY N/B", and an ordinary equity name — "Novo Nordisk A/S" —
    is returned whole because it never hits one.

    Args:
        name: The security name as the file wrote it.
        max_tokens: Cap on the words kept, for names with a long
            preamble.

    Returns:
        The head of the name, or ``""`` when nothing survives (a name
        that is all numbers is not a name to search on).
    """
    if not name:
        return ""
    out: list[str] = []
    for tok in str(name).split():
        # A token counts as a word when it holds no digits. "N/B" and
        # "A/S" survive; "4.25%", "15/11/2034" and "2031" stop the scan.
        if any(ch.isdigit() for ch in tok):
            break
        out.append(tok)
        if len(out) >= max_tokens:
            break
    return " ".join(out).strip()


def search_id_variant(identifier: str) -> str | None:
    """Resolve a CUSIP or ISIN to a Yahoo ticker via Yahoo's search endpoint.

    Passes the identifier directly to ``yfinance.Search`` as the query.
    Yahoo's search understands both CUSIP (9-character alphanumeric) and
    ISIN (12-character, two-letter country prefix) and typically returns
    the primary listing as the first result. No prefix matching is applied
    — the identifier is unambiguous, so the first returned ticker is used.

    This is intentionally simpler than :func:`search_name_variant` because:

    * CUSIPs and ISINs are exact identifiers; the first search result is
      virtually always the right security.
    * There is no ticker-prefix to match against — the caller has no ticker
      at all (that's the reason this path is being tried).

    Called by :func:`porxpy.extractors.get_symbol_info_cached` when no
    ticker is available but a CUSIP or ISIN is present on the row.

    Args:
        identifier: A CUSIP (9 chars) or ISIN (12 chars). Passed verbatim
            to ``yf.Search``; leading/trailing whitespace is stripped.

    Returns:
        The first Yahoo ticker returned by the search, or ``None`` if the
        search fails, returns nothing, or the first result has no symbol.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    try:
        import yfinance as yf
        result = yf.Search(identifier, max_results=3, news_count=0)
        quotes = result.quotes or []
    except Exception as exc:
        print(f"[IdSearch] yf.Search('{identifier}') failed: {exc}")
        return None

    for q in quotes:
        sym = (q.get("symbol") or "").strip()
        if sym:
            return sym
    return None
