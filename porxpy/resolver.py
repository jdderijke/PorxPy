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
        print(f"[Resolve] {isin}/{mic or '?'} → {known_ticker} [hint]")
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


def clean_holding_ticker_input(raw: str | None) -> str:
    """First-pass cleanup of an issuer-supplied ticker string.

    Trims whitespace, uppercases, strips a leading ``$``, and drops a
    trailing Bloomberg asset-class qualifier (``" Equity"`` and friends).
    Does NOT attempt to rewrite Bloomberg/Refinitiv/concat forms — those
    are handled by :func:`candidate_variants` and gated by Yahoo's
    response.

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
