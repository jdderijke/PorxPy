"""
Fund-house document adapters — fetch the issuer's own latest factsheet
and holdings file, and feed both into the machinery that already exists.

Why this exists
---------------
Everything PorxPy knows about a fund beyond Yahoo's thin profile comes
from two documents the issuer publishes and revises on a schedule: the
monthly factsheet and the daily (or monthly) holdings file. Until now
both arrived by hand — the user found the file on the issuer's site,
downloaded it, dropped it into a dialog, mapped its columns, and did the
whole thing again next month. The mapping was remembered; the errand was
not.

An adapter is the missing half: it knows where one fund house publishes
those two documents, so the errand becomes a button. Nothing downstream
changes — the factsheet goes through :func:`porxpy.utils.factsheet_put`
and the same AI extraction the upload dialog uses, and the holdings file
goes through :func:`porxpy.upload.upload_preview` and
:func:`porxpy.upload.upload_commit` with the mapping the user entered
last time. This module locates and fetches; it parses nothing and stores
no fund data of its own.

The split that makes six houses possible
----------------------------------------
Locating a document is the only part that differs per house, and it is
also the fragile part: issuer sites change, and a scraper written today
is a scraper that breaks. So the fragile part is small, isolated per
house, and always optional — :class:`IssuerAdapter` on its own can
already fetch from a URL the user has used before, which works for every
house on earth and needs no knowledge of any of them. A house adapter
only has to do better than that.

Six houses are registered (:data:`ADAPTERS`). A house is described by a
TABLE — :attr:`IssuerAdapter.DOCUMENTS`, a few class attributes, and
nothing else in the normal case — because every house answers the same
two questions and differs only in the strings. :func:`expand_specs` is
the one routine that turns those strings into candidates, shared by all
of them.

iShares discovers both documents; Xtrackers discovers its holdings and
is a pure table with not a line of code. The remaining four identify
their own funds and fall back to the remembered URL, which is a real
capability rather than a placeholder — a Vanguard fund whose holdings
URL the user pasted once refreshes from that URL for ever after.

Degrading is the normal case, not the error case
------------------------------------------------
Three things here are allowed to be absent, and none of them may fail
the whole operation:

* **The AI helper.** Extraction needs the Settings toggle AND an API key,
  and is off by default. Without it the adapter still fetches the
  factsheet, stores it against the fund and remembers where it came
  from — which is most of the value, and leaves the user one click from
  extracting later. "No API key" is reported as a step SKIPPED, never as
  a failure.
* **A remembered column mapping.** Without one there is nothing to parse
  a holdings file with, so that half is skipped and says so. The
  factsheet half still runs.
* **The issuer's site.** A house we cannot locate a document for reports
  that it could not, and the other half still runs.

The report this module returns therefore always describes both halves
separately, and a caller must never read "one half skipped" as "the run
failed".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from porxpy.config import CACHE_DIR
from porxpy.utils import now_iso


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
# Issuer sites sit behind bot protection that rejects a plain Python
# client outright — iShares answers 403 to `requests` and to curl, and
# 200 to a browser. The difference is the TLS handshake and the header
# set, not the IP, so the fix is the one this project already owns:
# curl_cffi's browser impersonation, pinned to the same profile and the
# same CA bundle `yf_session` chose for Yahoo.
#
# Reusing that module rather than picking a profile here is deliberate.
# Its choice encodes two hard-won facts about THIS machine — which
# impersonation profile survives the local middlebox, and which roots the
# operating system trusts — and a second, independent choice would be
# right on the developer's machine and wrong on the user's.
#
# Certificate verification stays ON, here as there. Issuer documents are
# the input to holdings and breakdowns; accepting one from an unverified
# peer would be worse than not fetching it.
_SESSION = None

# A fetch that has not answered in this long has failed. Holdings files
# run to several MB for a bond fund, hence the generosity.
HTTP_TIMEOUT = 120

# What a document may weigh before we refuse it. A holdings CSV for a
# 9,000-line aggregate bond fund is ~2MB; 64MB is far past any real
# document and stops a redirect-to-video from being written to disk.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


def _session():
    """The impersonating HTTP session, made once and reused.

    Returns:
        A ``curl_cffi`` session configured exactly as the Yahoo one is.

    Raises:
        RuntimeError: When curl_cffi is not installed. It ships with
            yfinance, so this is a broken install rather than an
            optional feature — said plainly instead of surfacing as an
            ImportError from inside a request handler.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        from curl_cffi import requests as _cr
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            "curl_cffi is not installed; it normally arrives with "
            "yfinance. Reinstall requirements.txt.") from exc
    from porxpy.yf_session import ca_bundle, impersonate_target
    _SESSION = _cr.Session(impersonate=impersonate_target(),
                           verify=ca_bundle() or True)
    return _SESSION


@dataclass
class Fetched:
    """One downloaded document.

    Attributes:
        url: Where it actually came from, after redirects.
        filename: The name the server gave it (Content-Disposition), or
            the last path segment. This matters more than it looks:
            :func:`porxpy.upload.upload_preview` types a file by its
            extension, and the factsheet store keys its extension off
            the same name.
        content_type: The declared MIME type, lower-cased and stripped
            of parameters.
        data: The bytes.
    """
    url: str
    filename: str
    content_type: str
    data: bytes


def http_get(url: str, *, referer: str = "") -> Fetched:
    """Fetch one document, or raise.

    Args:
        url: Absolute http(s) URL.
        referer: Sent as the Referer header. Several issuer download
            endpoints answer only when the request looks like it came
            from the product page, which is what this is for.

    Returns:
        A :class:`Fetched`.

    Raises:
        RuntimeError: On a non-200, a body too large to be a document,
            or a transport failure. The message names the URL, because
            an issuer URL that stopped working is the single most likely
            thing to go wrong here and the user can check it in a
            browser.
    """
    headers = {"Referer": referer} if referer else {}
    try:
        r = _session().get(url, timeout=HTTP_TIMEOUT, headers=headers,
                           allow_redirects=True)
    except Exception as exc:
        raise RuntimeError(f"could not reach {url}: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered HTTP {r.status_code}")
    data = r.content or b""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise RuntimeError(f"{url} returned {len(data):,} bytes, which is "
                           f"too large to be a fund document")
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()

    # Content-Disposition names the file the issuer thinks it is serving,
    # and it is routinely better than the URL: iShares' holdings link
    # ends in ".ajax" and the disposition says "IWDA_holdings.csv". The
    # extension decides how upload_preview parses it, so taking the URL's
    # would send a CSV down the "unsupported format" path.
    name = ""
    disp = r.headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp, re.I)
    if m:
        name = m.group(1).strip()
    if not name:
        name = Path(urlparse(str(r.url) or url).path).name or "document"
    return Fetched(url=str(r.url) or url, filename=name,
                   content_type=ctype, data=data)


# ---------------------------------------------------------------------------
# What an adapter is asked about, and what it answers
# ---------------------------------------------------------------------------
DOCUMENT_KINDS: tuple[str, ...] = ("factsheet", "holdings")


@dataclass
class FundRef:
    """Everything an adapter may look at to locate a fund's documents.

    Assembled by :func:`fund_ref`, never by an adapter — an adapter that
    read the cache itself would be a second answer to "what is this
    fund", and the two would drift.

    Attributes:
        isin: Fund ISIN, upper-case. The identifier every house indexes
            by, and the only one worth searching on.
        ticker: The listing the user is looking at. Useful as a hint
            (iShares names some documents after a ticker) and never as
            an identity — one fund has several.
        name: The fund's long name, as Yahoo has it.
        remembered: ``{kind: {source_value, source_kind, filename}}`` —
            where the user last got each document from. A ``url`` here
            is the strongest signal there is: the user has already
            confirmed that address serves this fund's document.
        factsheet_filename: The stored factsheet's filename, if any.
            Kept apart from ``remembered`` because it survives a
            drag-and-drop upload, which records no source at all, and
            for iShares the filename IS the document's slug on the
            issuer's site.
    """
    isin: str
    ticker: str = ""
    name: str = ""
    remembered: dict = field(default_factory=dict)
    factsheet_filename: str = ""

    def remembered_url(self, kind: str) -> str:
        """The remembered source for ``kind`` when it is an http URL.

        A remembered *disk path* is deliberately not offered: it names a
        file that was current when it was dropped and has not moved
        since, so re-reading it would report last month's holdings as
        this month's — the exact failure this feature exists to end.
        """
        rec = (self.remembered or {}).get(kind) or {}
        val = str(rec.get("source_value") or "").strip()
        return val if val.lower().startswith(("http://", "https://")) else ""

    def remembered_filename(self, kind: str) -> str:
        """The name of the file the user last used for ``kind``."""
        rec = (self.remembered or {}).get(kind) or {}
        return str(rec.get("filename") or "").strip()


@dataclass
class Candidate:
    """One place a document might be, and why we think so.

    Candidates are tried in order and the first that yields a plausible
    document wins. ``why`` is carried all the way to the user because
    "we used the URL you pasted last March" and "we found this on the
    issuer's product page" are different levels of confidence, and a
    wrong document is much easier to spot when you can see where it came
    from.
    """
    url: str
    why: str
    referer: str = ""


# ---------------------------------------------------------------------------
# The adapters
# ---------------------------------------------------------------------------
class IssuerAdapter:
    """Base adapter: locate a fund's documents at a fund house.

    On its own this implements the one strategy that works everywhere —
    *fetch it again from where you got it last time* — which is why it is
    a usable adapter rather than an abstract class. A house subclass adds
    discovery on top and keeps the fallback underneath it.

    Subclasses set :attr:`key`, :attr:`label`, and at least one of
    :attr:`name_patterns` / :attr:`hosts` so :func:`adapter_for` can
    recognise their funds; they override :meth:`discover` if they can do
    better than the remembered URL.
    """

    key: str = "generic"
    label: str = "the issuer"
    # Lower-case substrings of a fund's NAME that identify this house.
    name_patterns: tuple[str, ...] = ()
    # Host suffixes that identify this house in a remembered URL. A
    # matching host is stronger evidence than a matching name: the user
    # has been to that site for this fund.
    hosts: tuple[str, ...] = ()
    # Said in the report when this adapter has no discovery of its own,
    # so "nothing found" reads as a known limit rather than a failure.
    discovery_note: str = ""

    def matches(self, ref: FundRef) -> bool:
        """Whether this house publishes this fund."""
        name = (ref.name or "").lower()
        if any(p in name for p in self.name_patterns):
            return True
        for kind in DOCUMENT_KINDS:
            host = urlparse(ref.remembered_url(kind)).netloc.lower()
            if host and any(host == h or host.endswith("." + h)
                            for h in self.hosts):
                return True
        return False

    def sites(self) -> list[str]:
        """This house's site roots, in the user's own priority order.

        Read from settings on every call rather than cached, so a
        reorder on the Settings page takes effect on the next press of
        the button and not on the next restart.
        """
        from porxpy.utils import issuer_sites
        return issuer_sites(self.key)

    # How much of a URL's path identifies a national site. Zero for a
    # house that serves everything from one document domain; three for
    # iShares (country / investor type / language); one for a house
    # whose locale is a single segment. Configuration rather than an
    # override, because it is a number about a house, not behaviour.
    SITE_PATH_SEGMENTS: int = 0

    def site_base_from_url(self, url: str) -> str:
        """Reduce a document URL to the site root it belongs to.

        Used to LEARN a site from a URL the user supplied: they have
        just told us, by using it, that this house serves documents from
        this address, and the site list is how that knowledge reaches
        the next fund.

        How much of the path to keep is :attr:`SITE_PATH_SEGMENTS`. Zero
        collapses every market into one entry, which is right for a
        house with one document domain and wrong for one whose national
        sites are path prefixes — the distinction the site list exists
        to draw.
        """
        p = urlparse(url or "")
        if not (p.scheme and p.netloc):
            return ""
        root = f"{p.scheme}://{p.netloc}"
        n = self.SITE_PATH_SEGMENTS
        if n <= 0:
            return root
        parts = [seg for seg in p.path.split("/") if seg][:n]
        return root + ("/" + "/".join(parts) if len(parts) == n else "")

    # Where this house publishes, as DATA rather than as code.
    #
    # Every house answers the same two questions — "what URL is the
    # document at" and "what page lists the download links" — and the
    # answers differ only in the strings. So the strings are the
    # adapter: :func:`expand_specs` below is the one routine that turns
    # them into candidates, shared by every house, and a new house is
    # normally a table rather than a method.
    #
    # ``{kind: (spec, ...)}``, tried in order. A spec is one of:
    #
    #   Direct — the document's own address::
    #
    #       {"url": "{base}/literature/fact-sheet/{stem}.pdf",
    #        "why": "the {site} literature page",
    #        "needs": ("stem",)}
    #
    #   Scraped — a page that lists downloads::
    #
    #       {"page":    "{host}{product_url}",
    #        "link":    r'href="([^"]*fileName=[^"]*)"',
    #        "exclude": r"collateralSnapshot",
    #        "rank":    "family",
    #        "suffix":  "&siteEntryPassthrough=true",
    #        "why":     "the {filename} download on the {site} page"}
    #
    # Placeholders come from :meth:`facts`. ``needs`` names the facts a
    # spec cannot do without, so a spec is skipped rather than expanded
    # into a URL with a hole in it.
    DOCUMENTS: dict = {}

    # Facts that vary per SITE rather than per fund, keyed by the site's
    # own label (its first path segment). A house whose download URLs
    # carry country and language codes says them here instead of in
    # code: Xtrackers wants NLD/NLD, GBR/ENG, DEU/DEU, and those are
    # three lines of table rather than a method.
    SITE_FACTS: dict = {}

    # What to say when a kind of document cannot be found and there is
    # nothing to try. Per kind, because a house can discover one and not
    # the other — Xtrackers publishes its constituents at a predictable
    # address and its factsheets behind an opaque id.
    DISCOVERY_NOTES: dict = {}

    def facts(self, base: str, ref: FundRef) -> dict | None:
        """Placeholder values for this fund at this site.

        The one place a house may need real code, because "what is this
        fund called on this site" is the question no template can
        answer: iShares keys its product pages by an internal numeric
        id and names its documents after the LOCAL listing's ticker, so
        it has to consult that site's fund list. A house whose URLs are
        derivable from the ISIN needs none of this and inherits.

        Returns:
            A mapping of placeholder names to strings, or ``None`` when
            this site does not carry this fund at all — which is an
            ordinary answer, not an error, and simply produces no
            candidates for that site.
        """
        stem = re.sub(r"^drop_[0-9a-f]{8}_", "",
                      Path(ref.factsheet_filename or "").stem)
        return {
            "base":      base.rstrip("/"),
            "host":      f"{urlparse(base).scheme}://{urlparse(base).netloc}",
            "site":      _site_label(base),
            "isin":      ref.isin,
            "ticker":    ref.ticker,
            "name":      ref.name,
            "name_slug": _slugify(ref.name),
            "stem":      stem,
            **(self.SITE_FACTS.get(_site_label(base)) or {}),
        }

    def discover_at(self, base: str, ref: FundRef,
                    kind: str) -> list[Candidate]:
        """This house's documents of one kind, at ONE site.

        Shared by every house and not normally overridden: what differs
        between houses is :attr:`DOCUMENTS` and, where a site has to be
        asked what it calls a fund, :meth:`facts`.

        Anything raised is caught by :meth:`candidates`, recorded
        against that site, and the walk continues to the next one: a
        site that is down, or that has changed its markup, must cost the
        user that site rather than the whole run.
        """
        specs = self.DOCUMENTS.get(kind) or ()
        if not specs:
            return []
        f = self.facts(base, ref)
        if f is None:
            return []
        return expand_specs(specs, f, ref, kind)

    def candidates(self, ref: FundRef, kind: str) -> tuple[list[Candidate], str]:
        """Everywhere this document might be, best first.

        Two strategies, in this order:

        1. **The remembered URL.** The user's own confirmed answer for
           THIS fund, ahead of anything inferred: when the two disagree,
           the person who went and found the document is likelier to be
           right than our model of a fund house.
        2. **Discovery, site by site, in the user's priority order.**
           A fund house runs one site per market and each lists only the
           share classes registered for sale there, so a fund missing
           from the first site is an ordinary outcome rather than a
           failure — the walk simply continues. (Concretely: iShares
           Core MSCI World's factsheet is found on the UK site and not
           the Dutch one, and the STOXX Europe 600 ETF's on the Dutch
           site and not the UK one, because each is named after the
           local listing's ticker.)

        Returns:
            ``(candidates, note)``. ``note`` accounts for whatever went
            wrong while discovering, naming the site it went wrong at.
            It is a note rather than an exception because one broken
            site leaves every other candidate perfectly usable.
        """
        out: list[Candidate] = []
        url = ref.remembered_url(kind)
        if url:
            out.append(Candidate(url, "the URL you used last time"))

        problems: list[str] = []
        for base in self.sites():
            try:
                out.extend(self.discover_at(base, ref, kind))
            except Exception as exc:                 # noqa: BLE001
                problems.append(f"{_site_label(base)}: "
                                f"{type(exc).__name__}: {exc}")
        note = "; ".join(problems)
        if not out and not note:
            note = self.DISCOVERY_NOTES.get(kind) or self.discovery_note
        # De-duplicate while keeping order: two sites routinely offer the
        # same document, and discovery often re-finds the remembered URL.
        # Fetching either twice would only slow the run down.
        seen, uniq = set(), []
        for c in out:
            if c.url in seen:
                continue
            seen.add(c.url)
            uniq.append(c)
        return uniq, note



def expand_specs(specs, facts: dict, ref: FundRef, kind: str) -> list:
    """Turn a house's document specs into candidates. The whole engine.

    One routine for every fund house, because the houses differ in
    their STRINGS and not in what is done with them: a document is
    either at an address you can write down, or listed on a page you
    have to read. Everything past that — placeholder substitution,
    skipping a spec whose facts are missing, ranking the links a page
    offers — is the same work whoever published the file.

    Args:
        specs: The ``DOCUMENTS[kind]`` tuple for one house.
        facts: Placeholder values from :meth:`IssuerAdapter.facts`.
        ref: The fund, for the one ranking rule that needs it.
        kind: The document kind, used only in messages.

    Returns:
        Candidates in spec order, and within a scraped spec in ranked
        order.
    """
    out: list[Candidate] = []
    for spec in specs:
        needs = spec.get("needs") or ()
        if any(not str(facts.get(n) or "").strip() for n in needs):
            # A spec whose facts are missing is not an error: "the
            # factsheet slug we already hold" simply has nothing to say
            # about a fund whose factsheet nobody has uploaded.
            continue
        why_tpl = spec.get("why") or kind
        if spec.get("url"):
            out.append(Candidate(
                url=_fill(spec["url"], facts), why=_fill(why_tpl, facts),
                referer=_fill(spec.get("referer") or "", facts)))
            continue

        page_url = _fill(spec["page"], facts)
        html = http_get(page_url).data.decode("utf-8", "replace")
        links = sorted(set(re.findall(spec["link"], html)))
        exclude = spec.get("exclude")
        want = _filename_family(ref.remembered_filename(kind))

        def _rank(href: str):
            # "family" ranks a download by whether it is the same KIND
            # of report the user's saved mapping was made against — the
            # part of an issuer filename after the last underscore. A
            # house that publishes one report per fund needs no ranking
            # and does not ask for it.
            if spec.get("rank") != "family":
                return (0,)
            return (0 if (want and _filename_family(
                _query_value(href, "fileName") or href) == want) else 1,)

        for href in sorted(links, key=_rank):
            if exclude and re.search(exclude, href):
                continue
            filename = _query_value(href, "fileName") or Path(
                urlparse(href).path).name
            # Filled once, here, with the link's own filename in the
            # facts. Filling `why` before the loop blanked {filename}
            # before it had a value — the candidate then read "the
            # download on the iShares nl product page", naming nothing.
            out.append(Candidate(
                url=urljoin(page_url, href) + (spec.get("suffix") or ""),
                why=_fill(why_tpl, {**facts, "filename": filename}),
                referer=page_url))
    return out


def _fill(template: str, facts: dict) -> str:
    """Substitute ``{placeholders}`` from ``facts``, leaving none behind.

    ``str.format_map`` with a defaulting mapping rather than
    ``str.format``: a template naming a placeholder this house does not
    provide should produce an empty string, not a KeyError from inside a
    request handler.
    """
    class _D(dict):
        def __missing__(self, key):        # noqa: D105
            return ""
    return str(template or "").format_map(_D(facts))


# --- iShares / BlackRock ---------------------------------------------------
# The one house with real discovery, and worth reading as the worked
# example for the next one.
#
# Four facts about ishares.com, each established by trying it:
#
# 1. Every page and document needs `siteEntryPassthrough=true`. Without
#    it the site serves an investor-type interstitial with HTTP 200, so
#    the failure is a page of HTML where a PDF was expected rather than
#    an error status. Everything this adapter builds carries the flag.
# 2. Each national site publishes a product screener listing ISIN ->
#    product-page URL for everything it sells. That is the discovery
#    step: no search, no guessing, ~1200-1400 funds in one request.
# 3. The product page carries its own download links, holdings CSV
#    included, with the fund's local ticker already in the filename.
# 4. Factsheets live at a stable literature path whose last segment is
#    `<local ticker>-<fund name>-fund-fact-sheet-<lang>-<country>`, and
#    ANY national site will serve ANY of those slugs. So the site a slug
#    is fetched from does not matter; the site the slug is BUILT from
#    matters entirely, because the ticker in it is the local listing's.
#
# The fourth fact is why this house needs the site list more than any
# other. iShares Core MSCI World is IWDA in Amsterdam and SWDA in
# London, and only `swda-…-en-gb.pdf` exists; the STOXX Europe 600 ETF
# is EXSA on the Dutch site and is not sold on the UK one at all. Two
# funds, two different sites, neither reachable from the other's.
_ISHARES_HOST = "https://www.ishares.com"

# How long a screener index stays usable. It is ~2-4MB per site and
# answers for every fund at once, so it is fetched once a day and shared
# by every fund the user refreshes. Cached under cache/ because it is a
# fetched artefact: losing it costs one request, and it must never be
# treated as user state.
_ISHARES_INDEX_TTL_HOURS = 24

# Where a site's fund list lives, tried in this order before falling
# back to scanning the site's home page for a link. The three spellings
# are the same page in three languages; a site that uses a fourth is
# found by the fallback rather than by adding to this list.
_ISHARES_LIST_PATHS = ("/products/etf-investments",
                       "/producten/etf-investments",
                       "/produkte/etf-investments")

_DCR_RE = re.compile(
    r"/templatedata/config/product-screener-v3/data/[^\"'&\\\s]+backend-config")
_NEW_API_RE = re.compile(
    r"https://[^\"'\s]*blk-product-screener-server/api/v1/product-screener/product-data")


class ISharesAdapter(IssuerAdapter):
    """iShares (BlackRock)."""

    key = "ishares"
    label = "iShares"
    name_patterns = ("ishares",)
    hosts = ("ishares.com", "blackrock.com")
    # country / investor type / language, e.g. /uk/individual/en
    SITE_PATH_SEGMENTS = 3

    # Where iShares publishes, as data. Both entries are ordinary
    # templates; the only thing this house needs code for is `facts`,
    # because its product pages are keyed by an internal numeric id and
    # its documents are named after the LOCAL listing's ticker.
    DOCUMENTS = {
        "factsheet": (
            # A stored factsheet's FILENAME is the slug, and the path is
            # stable, so the same address serves this month's edition.
            # Any national site serves any slug, so this works from
            # whichever site is being tried.
            {"url": "{base}/literature/fact-sheet/{stem}.pdf"
                    "?siteEntryPassthrough=true",
             "why": "the iShares literature page for this factsheet",
             "needs": ("stem",)},
            # Built from THIS site's own fund list. The ticker in the
            # slug is the local listing's, which is what makes the site
            # order matter: the same fund is `iwda-…-nl-nl`, which does
            # not exist, and `swda-…-en-gb`, which does.
            {"url": "{base}/literature/fact-sheet/"
                    "{ticker_slug}-{name_slug}-fund-fact-sheet-{locale}.pdf"
                    "?siteEntryPassthrough=true",
             "why": "the iShares {site} factsheet for {ticker}",
             "needs": ("ticker_slug", "name_slug", "locale")},
        ),
        "holdings": (
            # The product page lists its own downloads. Ranked by
            # family so the report the user's mapping was made against
            # is tried first; the caller checks each one against that
            # mapping regardless, so this is a ranking of guesses.
            {"page":    "{host}{product_url}?siteEntryPassthrough=true",
             "link":    r'href="([^"]*\.ajax\?[^"]*fileName=[^"]*)"',
             # Securities-lending collateral is not the fund's holdings.
             # It looks like them — ticker, name, weight — which is
             # exactly why it has to be excluded by name.
             "exclude": r"collateralSnapshot",
             "rank":    "family",
             "suffix":  "&siteEntryPassthrough=true",
             "why":     "the {filename} download on the iShares {site} "
                        "product page",
             "needs":   ("product_url",)},
        ),
    }

    def facts(self, base: str, ref: FundRef) -> dict | None:
        """This fund as THIS iShares site names it.

        The one thing templates cannot express for this house: the
        product page is keyed by an internal numeric id, and the
        factsheet slug carries the local listing's ticker. Both come
        from the site's own fund list.

        Returns:
            The base facts plus ``product_url``, ``ticker``,
            ``ticker_slug``, ``name_slug`` and ``locale``, or ``None``
            when this site does not list the fund — which is ordinary:
            a fund is registered for sale in some countries and not
            others.
        """
        entry = (self._index(base) or {}).get((ref.isin or "").strip().upper())
        if not entry:
            return None
        f = super().facts(base, ref) or {}
        ticker = str(entry.get("ticker") or "").strip()
        if ticker == "-":
            ticker = ""
        f.update({
            "product_url": str(entry.get("url") or ""),
            "ticker":      ticker,
            "ticker_slug": _slugify(ticker),
            "name_slug":   _slugify(str(entry.get("name") or "")),
            "locale":      str(entry.get("locale") or ""),
        })
        return f

    # -- the ISIN -> product page index, per site --------------------------
    def _screener_config(self, base: str) -> dict:
        """How to ask THIS site for its fund list.

        Two backends are in service across iShares' national sites, and
        which one a site runs is not derivable from its URL — the Dutch
        and German sites answer on a `.jsn` endpoint keyed by a
        `dcrPath`, the UK site on a newer `product-data` API keyed by
        country/language/siteName/userType. Both publish their own
        configuration inside the fund-list page, as the JSON the
        screener app is bootstrapped with, so this READS the answer
        instead of encoding a table of sites that would go stale
        silently.

        Returns:
            ``{"url": str, "params": dict, "style": "jsn"|"api",
            "country": str, "language": str}``.

        Raises:
            RuntimeError: When the site's fund list cannot be found or
                carries neither configuration. Caught per site by
                ``candidates()``, so it costs this site and not the run.
        """
        html = self._list_page(base)
        text = _unescape(html)

        m = _NEW_API_RE.search(text)
        if m:
            # The query parameters sit beside the url in the same
            # `productScreenerDataApi` block; read them from a window
            # around it rather than from the whole page, where the same
            # key names appear in a dozen unrelated blocks.
            i = text.find("productScreenerDataApi")
            win = text[max(0, i - 200):i + 900] if i >= 0 else text[:2000]
            params = {}
            for key in ("country", "language", "siteName", "userType"):
                mm = re.search(rf'"{key}"\s*:\s*"([^"]+)"', win)
                if mm:
                    params[key] = mm.group(1)
            if params.get("country") and params.get("siteName"):
                return {"url": m.group(0), "params": params, "style": "api",
                        "country": params.get("country", ""),
                        "language": params.get("language", "")}

        m = _DCR_RE.search(text)
        if m:
            dcr = m.group(0)
            # The dcrPath's first data segment is the site's language,
            # which is the half of the factsheet locale suffix the URL
            # path also carries. Read here so both styles answer the
            # same question the same way.
            lang = ""
            mm = re.search(r"/data/([a-z]{2})/", dcr)
            if mm:
                lang = mm.group(1)
            return {"url": f"{base}/product-screener/product-screener-v3.jsn",
                    "params": {"dcrPath": dcr}, "style": "jsn",
                    "country": "", "language": lang}

        raise RuntimeError("no product screener configuration on the fund list page")

    def _list_page(self, base: str) -> str:
        """The site's fund-list page, as HTML."""
        for path in _ISHARES_LIST_PATHS:
            try:
                return http_get(f"{base}{path}?siteEntryPassthrough=true"
                                ).data.decode("utf-8", "replace")
            except RuntimeError:
                continue
        # Fallback: the home page links to it under whatever that site
        # calls it. One extra request, and only on a site whose fund
        # list is not at any of the three known spellings.
        home = http_get(f"{base}/?siteEntryPassthrough=true"
                        ).data.decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+)"', home):
            if "etf-investments" not in href:
                continue
            url = href.split("#")[0].split("?")[0]
            if url.startswith("/"):
                url = f"{_ISHARES_HOST}{url}"
            if not url.lower().startswith("http"):
                continue
            return http_get(f"{url}?siteEntryPassthrough=true"
                            ).data.decode("utf-8", "replace")
        raise RuntimeError("could not find this site's fund list page")

    def _index(self, base: str) -> dict:
        """``{isin: {url, ticker, name, locale}}`` for one site's range."""
        slug = _cache_slug(base)
        cached = _issuer_cache_read(f"ishares_{slug}", _ISHARES_INDEX_TTL_HOURS)
        if cached is not None:
            return cached.get("funds") or {}

        cfg = self._screener_config(base)
        qs = "&".join(f"{k}={v}" for k, v in cfg["params"].items())
        got = http_get(f"{cfg['url']}?{qs}&siteEntryPassthrough=true")
        payload = json.loads(got.data.decode("utf-8", "replace"))

        # The factsheet slug's locale suffix. Language from the site's
        # own configuration; country from the same place when it says
        # (the UK site calls itself "gb", which its URL path spells
        # "uk" — the difference is exactly why this is read rather than
        # derived), else from the URL path's first segment.
        path_parts = [p for p in urlparse(base).path.split("/") if p]
        lang = cfg.get("language") or (path_parts[-1] if path_parts else "")
        country = cfg.get("country") or (path_parts[0] if path_parts else "")
        locale = f"{lang}-{country}".lower() if lang and country else ""

        rows = _ishares_rows(payload, cfg["style"])
        funds: dict[str, dict] = {}
        for rec in rows:
            isin = str(rec.get("isin") or "").strip().upper()
            page = str(rec.get("productPageUrl") or "").strip()
            if not isin or not page:
                continue
            funds[isin] = {
                "url":    page,
                "ticker": str(rec.get("localExchangeTicker") or "").strip(),
                "name":   str(rec.get("fundName") or "").strip(),
                "locale": locale,
            }
        if not funds:
            raise RuntimeError("the product screener returned no funds")
        _issuer_cache_write(f"ishares_{slug}", {"funds": funds, "base": base})
        return funds

# --- The five houses that are declared but do not yet discover -------------
# Each of these recognises its own funds and inherits the remembered-URL
# strategy, so a fund whose document URL the user has pasted once already
# refreshes from it. What they do not have is discovery, and each says so
# in its own words rather than reporting a blank.
#
# They are registered now, rather than added later, for the reason the
# consistency rule in CLAUDE.md gives: a set with one member gets an
# interface shaped around that member. Six members shaped `discover()`
# into something a house can implement without touching anything else,
# and made the remembered-URL fallback belong to the base class, where
# every house gets it, instead of to iShares.
_NOT_YET = ("PorxPy cannot yet find {label}'s documents by itself. Upload "
            "the factsheet or the holdings file once from its URL and this "
            "button will re-fetch that address from then on.")


class _UndiscoveredHouse(IssuerAdapter):
    """A house PorxPy recognises but cannot yet browse."""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.discovery_note = _NOT_YET.format(label=cls.label)


class VanguardAdapter(_UndiscoveredHouse):
    key = "vanguard"
    label = "Vanguard"
    name_patterns = ("vanguard",)
    hosts = ("vanguard.com", "vanguard.co.uk", "vanguard.nl",
             "fund-docs.vanguard.com", "vanguardinvestor.co.uk")


class AmundiAdapter(_UndiscoveredHouse):
    key = "amundi"
    label = "Amundi"
    # Lyxor is Amundi since 2022 and its funds still carry the old name
    # on Yahoo, so both spellings reach this adapter.
    name_patterns = ("amundi", "lyxor")
    hosts = ("amundi.com", "amundietf.com", "lyxoretf.com")


class XtrackersAdapter(IssuerAdapter):
    """Xtrackers (DWS).

    The whole adapter, and deliberately not a line of code: everything
    that differs from iShares is a string in a table.

    **Holdings are a plain template.** DWS exports constituents at a
    predictable address keyed by the ISIN, so no page has to be read and
    no index consulted — which is why this house needs no ``facts()``
    override. The country/language codes in the path come from
    :attr:`SITE_FACTS`, and are not guessable: the German site wants
    DEU/**DEU**, not the DEU/GER its language name suggests.

    **Factsheets are not.** They are served from
    ``/download/asset/<guid>`` behind an opaque identifier with nothing
    in it derived from the fund, and the mapping from ISIN to guid lives
    in an API this adapter could not find. So a factsheet is fetched
    from the URL the user supplied for that fund — the base class's
    strategy, which is exactly what it is for — and the note below says
    so rather than reporting an empty result.
    """

    key = "xtrackers"
    label = "Xtrackers"
    # "DWS" is the parent and appears on the funds' own documents.
    name_patterns = ("xtrackers", "db x-trackers")
    hosts = ("xtrackers.com", "etf.dws.com", "dws.com")
    # etf.dws.com/nl-nl, /en-gb, /de-de — one segment, unlike iShares.
    SITE_PATH_SEGMENTS = 1

    SITE_FACTS = {
        "nl-nl": {"cc": "NLD", "lang": "NLD"},
        "en-gb": {"cc": "GBR", "lang": "ENG"},
        # Measured, not derived: DEU/GER answers 404 and DEU/DEU works.
        "de-de": {"cc": "DEU", "lang": "DEU"},
        "en-lu": {"cc": "LUX", "lang": "ENG"},
    }

    DOCUMENTS = {
        "holdings": (
            # A real .xlsx (a zip), named Constituent_<ISIN>.xlsx. The
            # CSV beside it is offered second: same data, and the xlsx
            # is what the site's own download button produces, so it is
            # the file a user's saved mapping is most likely made
            # against.
            {"url": "{host}/etfdata/export/{cc}/{lang}/excel/product/"
                    "constituent/{isin}/",
             "why": "the Xtrackers {site} constituents export",
             "needs": ("cc", "lang", "isin")},
            {"url": "{host}/etfdata/export/{cc}/{lang}/csv/product/"
                    "constituent/{isin}/",
             "why": "the Xtrackers {site} constituents CSV",
             "needs": ("cc", "lang", "isin")},
        ),
    }

    DISCOVERY_NOTES = {
        "factsheet": (
            "Xtrackers serves factsheets from an opaque download id that "
            "cannot be derived from the fund. Upload this fund's factsheet "
            "once from its URL (right-click the download on the product "
            "page and copy the link) and this button will re-fetch that "
            "address from then on."),
    }


class VanEckAdapter(_UndiscoveredHouse):
    key = "vaneck"
    label = "VanEck"
    name_patterns = ("vaneck", "van eck")
    hosts = ("vaneck.com",)


class SPDRAdapter(_UndiscoveredHouse):
    key = "spdr"
    label = "SPDR"
    # State Street's funds are branded SPDR; the manager's name appears
    # on the documents and in some Yahoo long names.
    name_patterns = ("spdr", "state street")
    hosts = ("ssga.com", "spdrs.com")


# Order matters only in that the first match wins, and no two houses
# claim the same name.
ADAPTERS: tuple[IssuerAdapter, ...] = (
    ISharesAdapter(),
    VanguardAdapter(),
    AmundiAdapter(),
    XtrackersAdapter(),
    VanEckAdapter(),
    SPDRAdapter(),
)

# The fallback for a fund from a house nobody has written an adapter for.
# It is a real adapter, not a null one: it re-fetches whatever URL the
# user last used, which is the whole feature for anyone who pastes URLs.
GENERIC_ADAPTER = IssuerAdapter()
GENERIC_ADAPTER.discovery_note = (
    "PorxPy does not recognise this fund's issuer. Upload the factsheet "
    "or the holdings file once from its URL and this button will "
    "re-fetch that address from then on.")


def house_for_url(url: str) -> IssuerAdapter | None:
    """The house whose site this URL belongs to, by HOST alone.

    Deliberately stricter than :func:`adapter_for`, which will also
    match on a fund's name. This one decides whether to write something
    into a user's settings, and a name like "Vanguard S&P 500" appearing
    in a URL from an unrelated data provider must not file that provider
    under Vanguard. A host match is evidence about the site; a name
    match is only evidence about the fund.
    """
    host = urlparse(url or "").netloc.lower()
    if not host:
        return None
    for a in ADAPTERS:
        if any(host == h or host.endswith("." + h) for h in a.hosts):
            return a
    return None


def learn_site(url: str) -> str:
    """Record the site root of a URL the user has just used.

    Called from the one place a remembered upload source is written
    (:func:`porxpy.utils.upload_source_put`), so every dialog that can
    take a URL — holdings, factsheet, breakdown CSV — teaches the site
    list without any of them knowing that it does.

    This is how the fallback list grows into the markets a particular
    user actually buys in. Someone whose funds are all LSE-listed will
    paste UK links, and the UK site arrives in their list on its own.

    Args:
        url: The source the user supplied. A filesystem path or a
            dropped-file scratch path is ignored — a path names a copy
            on this machine, not a place documents are published.

    Returns:
        The site root that was added, or ``""`` when nothing was (not a
        URL, not a recognised house, or already in the list).
    """
    if not str(url or "").lower().startswith(("http://", "https://")):
        return ""
    adapter = house_for_url(url)
    if adapter is None:
        return ""
    base = adapter.site_base_from_url(url)
    if not base:
        return ""
    from porxpy.utils import issuer_site_learn
    return base if issuer_site_learn(adapter.key, base) else ""


def adapter_for(ref: FundRef) -> IssuerAdapter:
    """The adapter that claims this fund, or the generic one.

    Never returns None. A fund with no recognised house still has the
    remembered-URL strategy available to it, and giving the caller an
    adapter that can do that is more useful than making every caller
    handle an absent one.
    """
    for a in ADAPTERS:
        if a.matches(ref):
            return a
    return GENERIC_ADAPTER


# ---------------------------------------------------------------------------
# Locating and fetching, as one operation
# ---------------------------------------------------------------------------
def fund_ref(isin: str, ticker: str = "", name: str = "") -> FundRef:
    """Assemble what the adapters need to know about one fund.

    The single place the stores are read for this purpose, so no adapter
    has to know where a remembered source lives or how a factsheet's
    filename is spelled.

    Args:
        isin: Fund ISIN. Required — every store here is ISIN-keyed.
        ticker: The listing the request came from.
        name: The fund's long name. Passed in by the caller, which has
            already loaded the fund, rather than re-read here.

    Returns:
        A populated :class:`FundRef`.
    """
    from porxpy.utils import factsheet_get, upload_source_get

    key = (isin or "").strip().upper()
    remembered = {}
    for kind in DOCUMENT_KINDS:
        rec = upload_source_get(key, kind) if key else None
        if rec:
            remembered[kind] = rec
    meta = (factsheet_get(key) if key else None) or {}
    return FundRef(isin=key, ticker=(ticker or "").strip(),
                   name=(name or "").strip(), remembered=remembered,
                   factsheet_filename=str(meta.get("filename") or ""))


# What each kind of document is allowed to look like. The check that
# matters is not the extension but the first few bytes, because the
# failure this guards against does not announce itself: iShares (and
# most issuer sites) answer an unauthorised or expired document request
# with an investor-type interstitial served as HTTP 200 text/html. Stored
# unchecked, that HTML becomes "the factsheet", and the AI helper is then
# paid to read a cookie disclaimer.
_PDF_MAGIC   = b"%PDF"
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")
_ZIP_MAGIC   = b"PK\x03\x04"        # xlsx is a zip


def implausible(kind: str, got: Fetched) -> str:
    """Why ``got`` is not a ``kind`` document, or ``""`` if it might be.

    Errs towards rejecting: a wrong document that is accepted replaces
    real data, while a right one that is rejected costs the user a
    manual upload they were doing anyway.
    """
    head = (got.data or b"")[:8]
    if not head:
        return "the download was empty"
    if kind == "factsheet":
        if head.startswith(_PDF_MAGIC) or head.startswith(_IMAGE_MAGIC):
            return ""
        return ("the issuer served a web page rather than a document — the "
                "link may have expired or now need a login")
    if kind == "holdings":
        if head.startswith(_ZIP_MAGIC):
            return ""
        if got.content_type in ("text/html", "application/xhtml+xml"):
            return "the issuer served a web page rather than a data file"
        # SpreadsheetML (an .xls extension over an XML document) is
        # readable since v0.106.1, and refusing it was not a small
        # limitation: it is the format iShares' own "Fund Download"
        # button produces, so it is the file a user is most likely to
        # have mapped by hand — and skipping it sent this walk on to a
        # DIFFERENT report whose columns then failed to match.
        from porxpy.upload import _looks_like_spreadsheetml
        if _looks_like_spreadsheetml(got.data):
            return ""
        if head.lstrip().startswith(b"<"):
            return "the issuer served markup rather than a data file"
        return ""
    return ""


@dataclass
class Located:
    """The outcome of looking for one document.

    Attributes:
        got: The document, or None when none of the candidates worked.
        why: Where the successful candidate came from, for the report.
        tried: One line per candidate that did not work, so a failed
            run says what was attempted rather than only that it failed.
        note: Discovery's own complaint, when it had one.
    """
    got: Fetched | None = None
    why: str = ""
    tried: list = field(default_factory=list)
    note: str = ""


def locate(ref: FundRef, adapter: IssuerAdapter, kind: str,
           accept=None) -> Located:
    """Find and download one of a fund's documents.

    Walks the adapter's candidates in order and returns the first that
    downloads and looks like the right kind of thing. A candidate that
    fails is recorded and the walk continues: the ordering is a ranking
    of guesses, and one bad guess should not end the search.

    Args:
        ref: The fund.
        adapter: Usually :func:`adapter_for`'s answer.
        kind: One of :data:`DOCUMENT_KINDS`.
        accept: Optional ``fn(Fetched) -> str`` giving the caller the
            last word on a download: ``""`` accepts it, any other string
            rejects it with that reason and the walk continues.

            This exists because "is this the right file?" is often only
            answerable by opening it. A fund house publishes several
            reports per fund and PorxPy's ordering of them is a guess;
            the holdings caller can settle it by checking the file
            against the column mapping it is about to apply, which is a
            fact rather than a guess.

    Returns:
        A :class:`Located`. Never raises for an ordinary "not found" —
        that is an answer, and the caller has another half of the job to
        get on with.
    """
    cands, note = adapter.candidates(ref, kind)
    out = Located(note=note)
    for c in cands:
        try:
            got = http_get(c.url, referer=c.referer)
        except RuntimeError as exc:
            out.tried.append(f"{c.why}: {exc}")
            continue
        bad = implausible(kind, got)
        if not bad and accept is not None:
            bad = accept(got) or ""
        if bad:
            out.tried.append(f"{c.why}: {bad}")
            continue
        out.got, out.why = got, c.why
        return out
    return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _query_value(url: str, key: str) -> str:
    """One query parameter's value, without parsing the whole URL.

    Deliberately not ``parse_qs``: these hrefs come out of HTML with
    ``&amp;`` intact in places, and a tolerant regex reads them where a
    strict parser reports an empty query.
    """
    m = re.search(rf"[?&](?:amp;)?{re.escape(key)}=([^&\"']*)", url)
    return m.group(1) if m else ""


def _unescape(html: str) -> str:
    """HTML entities resolved, for reading JSON out of an attribute.

    The screener's configuration is a JSON document embedded in an HTML
    attribute, so every quote in it arrives as ``&quot;``. Unescaping
    once up front is what lets the patterns below be written as the JSON
    they are matching rather than as its escaped form.
    """
    import html as _html
    return _html.unescape(html or "")


def _slugify(text: str) -> str:
    """Lower-case, hyphen-joined, alphanumerics only.

    The rule iShares names its literature files by: "iShares Core MSCI
    World UCITS ETF" becomes "ishares-core-msci-world-ucits-etf".
    """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _site_label(base: str) -> str:
    """A site root said the way a person would say it: "uk", "nl".

    Used in every message that names which site something came from or
    failed at, because a full URL in a sentence is unreadable and the
    country segment is the part that identifies it.
    """
    parts = [p for p in urlparse(base or "").path.split("/") if p]
    return parts[0] if parts else (urlparse(base or "").netloc or base or "?")


def _cache_slug(base: str) -> str:
    """A filesystem-safe name for one site's cached index."""
    p = urlparse(base or "")
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{p.netloc}{p.path}").strip("_").lower()


def _ishares_rows(payload, style: str) -> list[dict]:
    """One row per fund, from either screener backend.

    The two shapes are genuinely different — the older `.jsn` endpoint
    returns column names and parallel value arrays, the newer API a dict
    keyed by product id whose values are already records — so they are
    normalised here, once, and every caller downstream sees records.

    Values in the older shape may be a plain string or a
    ``{"d": display, "r": raw}`` pair; only the string fields are read,
    so the pairs are left alone rather than unwrapped.
    """
    if style == "api":
        return [v for v in (payload or {}).values() if isinstance(v, dict)]
    table = ((payload or {}).get("data") or {}).get("tableData") or {}
    names = [c.get("name") for c in (table.get("columns") or [])]
    return [dict(zip(names, row)) for row in (table.get("data") or [])]


def _filename_family(name: str) -> str:
    """The part of a download's name that says which layout it has.

    Issuer download filenames are ``<something specific to the
    fund>_<family>``: ``IWDA_holdings``, ``iShares-Core-MSCI-World-UCITS-
    ETF_fund``. The fund part changes per fund and the family part does
    not, so the family is what tells you two files share a layout — and
    therefore whether one file's column mapping fits the other.

    Returns:
        The lower-cased text after the last underscore, without an
        extension, or ``""`` when the name has no underscore.
    """
    stem = Path((name or "").strip()).stem
    return stem.rsplit("_", 1)[-1].lower() if "_" in stem else ""


# --- the issuer document cache --------------------------------------------
# One directory, one JSON file per index, each with the time it was
# fetched. Under cache/ and therefore losable by design: everything here
# can be fetched again, and nothing here is the user's.
#
# Not a `CACHE_CATEGORIES` entry, and that asymmetry is deliberate: the
# app's cache is keyed by ticker or ISIN and holds facts about ONE fund,
# while this holds one issuer's whole range and belongs to no fund. Bent
# into that store it would have to be filed under an arbitrary fund, and
# the next reader would rightly wonder why.
_ISSUER_CACHE_DIR = CACHE_DIR / "issuers"


def _issuer_cache_read(name: str, ttl_hours: float):
    """A cached issuer index, or ``None`` when missing or stale."""
    fp = _ISSUER_CACHE_DIR / f"{name}.json"
    try:
        blob = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        age = time.time() - float(blob.get("_fetched_epoch") or 0)
    except (TypeError, ValueError):
        return None
    return blob if age < ttl_hours * 3600 else None


def _issuer_cache_write(name: str, blob: dict) -> None:
    """Store an issuer index. Failure to cache is never fatal."""
    try:
        _ISSUER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = dict(blob)
        out["_fetched_epoch"] = time.time()
        out["_fetched_at"] = now_iso()
        (_ISSUER_CACHE_DIR / f"{name}.json").write_text(
            json.dumps(out), encoding="utf-8")
    except Exception as exc:                          # noqa: BLE001
        print(f"[Issuers] could not cache {name}: {exc}")
