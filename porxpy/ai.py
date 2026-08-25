"""
Factsheet extraction via the Anthropic API.

Turns an uploaded issuer factsheet into validated field values, facet
breakdowns and the position list it prints. One call per document
produces all three, so the metadata, the facets and the holdings provably
come from a single reading — two calls could disagree with each other
about the same page.

Why a language model rather than a parser
-----------------------------------------
The documents this exists for are the ones Yahoo covers badly, which in
practice means European UCITS funds whose factsheets are published in
whatever language and layout the issuer prefers. The Northern Trust sheet
used in development is Dutch: *Lopende kosten*, *Omvang fonds*,
*Luxe Consumentengoederen*. A regex parser would need per-issuer,
per-language rules and would break on every template change. Reading
prose is the one job a model does better than code.

What keeps it honest
--------------------
* **The prompt is generated from the registry.** Field names, types,
  vocabularies and bounds come from ``OVERRIDABLE_FIELDS`` and the
  resource CSVs, so the model can never be asked for something the store
  would reject, and adding an overridable field extends extraction with
  no change here.
* **Every value is validated on return** by the same
  ``coerce_override_value`` a hand-typed value goes through. A model
  answering ``market_cap: "enormous"`` is rejected by code that already
  existed.
* **Every value must carry a citation** — page and verbatim quote. A
  field without one is discarded before validation. Hallucinated numbers
  are well-formed and pass every type check; the quote is the only thing
  that distinguishes extraction from invention.
* **Units are declared, never inferred.** Every numeric answer states
  its unit and the conversion is explicit. Inferring scale from
  magnitude is what made total net assets wrong in three consecutive
  releases, and a factsheet saying "$11,7 miljard" is exactly that
  terrain.
* **Nothing is applied.** Results are stored against the factsheet and
  staged into the existing review UI; the user's Save commits them.
"""

from __future__ import annotations

import base64
import json
import re

from porxpy.config import (
    BREAKDOWN_FACETS,
    FACTSHEET_MIME,
    MARKET_CAP_BUCKETS,
    OVERRIDABLE_FIELDS,
    field_vocab,
)

# Anthropic API. The key is read from the environment, never from
# settings.json — that file sits in the project directory in plaintext
# and gets copied around, which is no place for a credential.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Per-million-token prices, for the cost estimate shown in the dialog.
# Approximate and local: this is "roughly what that cost", not billing.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
}

# Fields worth asking a factsheet about. Registry fields that have a
# payload target, minus the ones no factsheet states.
_SKIP_FIELDS = {"include_in_optimizer"}

MAX_DOC_BYTES = 24 * 1024 * 1024

# Field names a reply may plausibly use instead of the registry's own.
_FIELD_ALIASES: dict[str, str] = {
    # Renamed in v0.47.0 to separate it from the asset_class FACET.
    "asset_class":       "primary_asset_class",
    "fund_asset_class":  "primary_asset_class",
    # Occasionally offered under their display names.
    "ter":               "expenseRatioPct",
    "ongoing_charges":   "expenseRatioPct",
    "ocf":               "expenseRatioPct",
    "expense_ratio":     "expenseRatioPct",
    "total_net_assets":  "totalNetAssets",
    "fund_size":         "totalNetAssets",
    "turnover":          "turnoverPct",
    "portfolio_turnover": "turnoverPct",
}


def api_key_present() -> bool:
    """Whether an API key is available in the environment."""
    import os
    return bool((os.environ.get(API_KEY_ENV) or "").strip())


def _market_cap_definition() -> str:
    """The size bands, worded from :data:`MARKET_CAP_BUCKETS`.

    Generated rather than typed out for the reason the whole prompt is:
    the same three numbers decide how a *fetched* median market cap is
    bucketed, and a prompt that named different ones would have the model
    and the code disagree about what "mid" means — with the disagreement
    invisible, because both answers are well-formed.
    """
    parts: list[str] = []
    prev: float | None = None
    for name, floor in MARKET_CAP_BUCKETS:
        def _b(v: float) -> str:
            return f"${v / 1e9:g}bn"
        if prev is None:
            parts.append(f"{name} = {_b(floor)} and above")
        elif floor <= 0:
            parts.append(f"{name} = below {_b(prev)}")
        else:
            parts.append(f"{name} = {_b(floor)} up to {_b(prev)}")
        prev = floor
    return "; ".join(parts)


# Per-field guidance for the fields a generic "one of [vocab]" line does
# not describe well enough. Kept beside the generated lines rather than
# in the prompt body so a field's rule sits with the field.
def _field_note(name: str) -> str:
    """Extra instruction for one field, or "" for the ordinary ones."""
    if name == "primary_asset_class":
        return (" — the fund's OWN kind as a single value; NOT the "
                "asset_class facet listed further down, which is a "
                "breakdown of its holdings. Return both when the "
                "document supports both.")
    if name == "market_cap":
        # Factsheets state this two different ways and the two deserve
        # different answers. A market-cap EXPOSURE table describes the
        # fund's composition, so the biggest slice names the fund. A
        # weighted-average (or median) figure is a single number standing
        # in for a whole distribution — a total-market tracker holding
        # thousands of small caps still shows an average in the hundreds
        # of billions, because the average follows the mega-caps that
        # dominate the weighting. Reading a category off that number
        # would report the largest holdings as though they were the fund,
        # so an average alone answers "mixed": the document genuinely has
        # not said how the fund is distributed.
        return (
            f" — the fund's market-cap profile. Read it from a market-cap "
            f"EXPOSURE breakdown where the document has one (\"Market "
            f"capitalisation exposure\", \"Marktkapitalisierung\", "
            f"\"Capitalisation boursi\u00e8re\", or a giant/large/mid/small/"
            f"micro table) and answer with the band holding the LARGEST "
            f"exposure. The bands are company size in USD: "
            f"{_market_cap_definition()}; map the document's own buckets "
            f"onto them by those figures, so a \"giant\" or \"mega\" bucket "
            f"is large and a \"micro\" bucket is small. If the document "
            f"gives ONLY a weighted-average or median market cap and no "
            f"breakdown, answer \"mixed\" — an average is one number "
            f"standing in for a distribution and does not say which band "
            f"the fund sits in. Quote the table row, or the average, "
            f"accordingly.")
    return ""


def _field_spec_lines() -> list[str]:
    """One prompt line per extractable field, describing what to return."""
    lines: list[str] = []
    for name, spec in OVERRIDABLE_FIELDS.items():
        if name in _SKIP_FIELDS or name.startswith("breakdown_source."):
            continue
        if not spec.get("target"):
            continue
        kind = spec.get("type")
        label = spec.get("label") or name
        if kind == "enum":
            vocab = ", ".join(field_vocab(spec))
            lines.append(
                f'- "{name}" ({label}): one of [{vocab}]{_field_note(name)}')
        elif kind == "number":
            unit = spec.get("unit") or ""
            bits = []
            if spec.get("min") is not None:
                bits.append(f'min {spec["min"]}')
            if spec.get("max") is not None:
                bits.append(f'max {spec["max"]}')
            rng = f' ({", ".join(bits)})' if bits else ""
            hint = ('state unit as "percent"' if unit == "%"
                    else 'state unit as "units", "thousands", '
                         '"millions" or "billions"')
            lines.append(f'- "{name}" ({label}): a number{rng}; {hint}')
        else:
            lines.append(f'- "{name}" ({label}): a short string')
    return lines


def _facet_vocab_lines() -> list[str]:
    """One prompt block per facet, listing its canonical keys BY LEVEL.

    v0.60.0. Until this release the sector line offered the twelve
    sectors and nothing else, so a factsheet whose sector table read
    "Cyclical 10% / Defensive 81% / Sensitive 9%" had no legal way to be
    reported. The model did the only thing it could and guessed the
    nearest sector name — cyclical became "consumer cyclical",
    defensive became "consumer defensive", sensitive became
    "technology". Three sectors the document never mentioned, stored as
    though it had, with the printed label the only surviving evidence.

    That is the worst failure mode this pipeline has: not a gap, but
    invented data that looks exactly like real data. Offering every
    level is the fix, together with the instruction below never to move
    between them — a super sector is a real answer, and reporting it as
    a sector claims the document said more than it did.

    The vocabulary comes from ``facet_alias_targets``, the same function
    behind the Resolve dialog's dropdown. One authority for "what may
    this value legally be", rather than a second list here free to drift
    from it.
    """
    from porxpy.resources import CURRENCY_ROWS, facet_alias_targets

    def _cap(values, n=200):
        vals = [str(v) for v in values if v]
        return ", ".join(vals[:n])

    def _by_level(facet: str) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for t in facet_alias_targets(facet):
            grouped.setdefault(t["level"], []).append(t["key"])
        return grouped

    lines: list[str] = []

    ac = _by_level("asset_class").get("asset_class") or []
    lines.append(f'- "asset_class": [{_cap(ac)}]')

    sec = _by_level("sector")
    lines.append(
        '- "sector": a THREE-LEVEL taxonomy. Use whichever level the '
        'document itself names, and never convert between levels:')
    lines.append(f'    - super sectors: [{_cap(sec.get("super_sector") or [])}]')
    lines.append(f'    - sectors:       [{_cap(sec.get("sector") or [])}]')
    lines.append(f'    - sub sectors:   [{_cap(sec.get("sub_sector") or [])}]')

    lines.append(f'- "currency": ISO 4217 codes, e.g. '
                 f'[{_cap((r.get("code") for r in CURRENCY_ROWS), 12)}, …]')

    cty = _by_level("country")
    countries = cty.get("country") or []
    regions   = cty.get("region") or []
    if countries:
        lines.append(
            '- "country": TWO levels. Use whichever the document names, '
            'and never convert between them:')
        lines.append(f'    - regions:   [{_cap(regions)}]')
        lines.append(f'    - countries: Morningstar country keys, e.g. '
                     f'[{_cap(countries, 12)}, …]')
    else:
        lines.append('- "country": lowercase country names without spaces, '
                     'e.g. unitedstates, unitedkingdom')
    return lines


def _focus_vocab_lines() -> str:
    """The legal focus_detail values, per focus_type.

    Spelled out in the prompt because focus_detail's vocabulary depends
    on focus_type — the one cross-field rule in the registry, and one a
    generic per-field description cannot express. Without it the model
    returns a plausible name ("Europe", "World") that fails validation,
    and the field silently disappears from the result.
    """
    try:
        from porxpy.resources import (COUNTRY_ROWS, SECTORS_ROWS,
                                      focus_region_vocabulary)
        # Countries, regions and super regions - close to 300 values,
        # every one of which was pasted into every extraction prompt to
        # say what the sentence below says in a line. The coarse keys
        # are still listed, because those are the ones a factsheet
        # words its own way ("Eurozone", "Developed Europe"); a country
        # it simply names, and the instruction covers the spelling.
        _vocab  = list(focus_region_vocabulary())
        _known  = {r["name"] for r in COUNTRY_ROWS if r.get("name")}
        regions = ", ".join(v for v in _vocab if v not in _known)
        sectors = ", ".join(r.get("sector") for r in SECTORS_ROWS if r.get("sector"))
    except Exception:
        return ""
    return (f'  - focus_type "geography" -> focus_detail is a country key, '
            f'lowercase and unspaced (unitedstates, unitedkingdom, japan), '
            f'or one of these broader keys: [{regions}]\n'
            f'  - focus_type "sector"   -> focus_detail one of [{sectors}]\n'
            f'  - focus_type "thematic" -> focus_detail free text, e.g. '
            f'"artificial intelligence"\n'
            f'  - focus_type "none"     -> focus_detail ""')


def build_extraction_prompt() -> str:
    """The extraction instruction, generated from the live vocabularies."""
    fields = "\n".join(_field_spec_lines())
    facets = "\n".join(_facet_vocab_lines())
    focus_vocab = _focus_vocab_lines()
    return f"""You are reading an investment fund factsheet and extracting structured data from it.

Return ONE JSON object and nothing else. No prose, no markdown fences.

{{
  "as_of": "YYYY-MM-DD or null",
  "identity": {{
    "longName": "the fund's full name, or null",
    "isin":     "ISIN, or null",
    "ticker":   "exchange ticker, or null",
    "exchange": "listing exchange, or null",
    "currency": "share-class currency, or null"
  }},
  "fields": {{
    "<field name>": {{
      "value": <value>,
      "unit": "<unit, numeric fields only>",
      "page": <page number>,
      "quote": "<the exact text you read this from, verbatim>",
      "confidence": "high" | "medium" | "low"
    }}
  }},
  "facets": {{
    "<facet name>": {{
      "items": [{{"key": "<canonical key>", "weight": <percent 0-100>,
                  "label_in_document": "<the label as printed>"}}],
      "sum_pct": <the weights' total>,
      "complete": true | false,
      "page": <page number>,
      "quote": "<the table's heading, verbatim>"
    }}
  }},
  "holdings": {{
    "rows": [{{
      "name":     "<the position as printed>",
      "weight":   <percent of the fund, 0-100>,
      "ticker":   "<exchange ticker, if the table prints one>",
      "isin":     "<ISIN, if the table prints one>",
      "cusip":    "<CUSIP, if the table prints one>",
      "sector":   "<sector as printed, if the table has that column>",
      "country":  "<country as printed, if the table has that column>",
      "currency": "<currency as printed, if the table has that column>",
      "asset_class": "<asset class as printed, if the table has one>",
      "coupon":   <coupon percent, bond tables only>,
      "maturity": "<maturity date as printed, bond tables only>",
      "duration": <duration in years, bond tables only>
    }}],
    "complete": true | false,
    "page": <page number>,
    "quote": "<the table's heading, verbatim>"
  }}
}}

FIELDS you may return. Omit any the document does not state — do not guess:
{fields}

MAPPING HINTS for the closed vocabularies. Factsheets word these their own
way; map them onto the allowed values rather than omitting the field:

- replication: "physical", "physical replication", "full replication",
  "direct replication", "in-kind" -> full
  "optimised", "optimized", "sampling", "sampled", "representative
  sampling", "stratified" -> sampled
  "synthetic", "swap-based", "swap based", "unfunded swap", "funded swap"
  -> synthetic
  A non-ETF fund that simply holds securities directly -> full
- structure: "ETF", "exchange traded fund", "UCITS ETF" -> etf;
  "SICAV", "FCP", "OEIC", "unit trust", "common contractual fund",
  "mutual fund", "index fund", "beleggingsfonds" -> fund
- style: "index", "index-tracking", "passive", "tracker", "indexfonds"
  -> passive; "actively managed", "active", "stock picking" -> active
- distribution: "accumulating", "acc", "capitalisation", "accumuleren",
  "reinvested", "thesaurierend" -> accumulating;
  "distributing", "dist", "income", "uitkerend", "ausschuettend" -> distributing
- primary_asset_class: the fund's OWN kind, as one value — equity for a
  share fund, fixed_income for a bond fund, mixed for a multi-asset fund,
  commodity for gold or broad commodities, cash for a money-market fund.
  Not to be confused with the "asset_class" FACET below, which is the
  fund's holdings broken down by class and may have several rows. A
  60/40 fund is primary_asset_class "mixed", and its asset_class facet is
  roughly 60% equity / 40% fixed_income.
- focus_type / focus_detail: focus_type says WHAT KIND of concentration
  the fund has; focus_detail names it, and MUST be one of the keys listed
  below for that type. A global or world fund has no regional focus:
  return focus_type "none".
{focus_vocab}

FACETS you may return, with their canonical keys. Map the document's own
labels onto these keys; put the printed label in "label_in_document" so a
human can check the mapping.

Match the GRAIN the document uses. Where a facet has levels, a table
naming super sectors is answered with super sector keys, and a table
naming countries with country keys. Never roll a value up or down to
reach a different level: "Cyclical" is a super sector and must be
returned as "cyclical", NOT as "consumer cyclical" — that would claim
the document said something it did not. Reporting the level the document
actually used is always right, even when a finer level exists.

If a label has no reasonable canonical key at ANY level, still return it
with your best guess at "key" and the exact printed text in
"label_in_document":
{facets}

HOLDINGS is the fund's own position table, if the document prints one
("Top 10 holdings", "Grootste posities", "Principales positions",
"Largest holdings", or a full schedule of investments). Report the rows
EXACTLY as printed:

- One entry per printed position, in the document's own order. Do not
  merge, re-order, split or total them, and do not add a row for
  "Other" / "Cash" unless the table itself prints one as a position.
- "weight" is the position's percent of the FUND. If the table gives
  market values rather than percentages, report the values under
  "weight" only when the table also states the fund total and the
  percentages follow from it; otherwise omit the weight rather than
  computing one from a total you inferred.
- "name" is required; every other column is optional and must be
  omitted — not guessed, not left as an empty string — when the table
  does not print it. A holdings table with no sector column is the
  ordinary case, and PorxPy fills those gaps from its own lookups.
- Never invent a ticker, ISIN or CUSIP for a position that prints only
  a name. A wrong identifier is worse than no identifier: it sends the
  enrichment step off to a different security entirely.
- sector / country / currency / asset_class are reported HERE as the
  document prints them, unmapped — unlike the facet tables above.
  PorxPy resolves each against its own vocabulary and keeps the printed
  text beside the result, so an unfamiliar label can be taught later
  rather than being guessed at now.
- "complete" is false for any table headed "top", "largest", "principal",
  "grootste", "principaux" or similar, and for any table whose weights
  fall noticeably short of 100. A top-10 presented as the whole fund
  misstates every position in it.
- Omit "holdings" entirely when the document prints no position table.
  An absent list is a correct answer; an invented one is not.

IDENTITY is read back for cross-checking only — PorxPy will not overwrite
a fund's identity from a document, but a mismatch tells the user the
factsheet belongs to a different share class. Report what the document
says, not what you think it should say.

RULES

1. Every entry in "fields" MUST have a "page" and a verbatim "quote"
   showing where you read it. If you cannot quote it, omit the field.
   Never infer a value from another value.

2. Numeric fields MUST state a "unit". A factsheet may say "0,07%",
   "$11,7 miljard", or "84.138,91" — report the number as printed
   (converted to a plain decimal, so "11,7" becomes 11.7) and say which
   unit it is in. Do not convert between units yourself.

3. "complete" on a facet means the table accounts for the whole fund.
   Set it FALSE when the heading says "largest", "top", "principal",
   "grootste", "principaux" or similar, or when the weights sum to
   noticeably less than 100. This matters: a partial table treated as
   complete misstates every holding in it.

4. Use the fund's own column where a table shows both fund and benchmark.

5. The document may be in any language. Map its labels onto the English
   canonical keys above.

6. Return only what the document states. An empty "fields" or "facets"
   object is a correct answer for a document that says nothing."""


def _media_type(ext: str) -> str:
    return FACTSHEET_MIME.get((ext or "").lower(), "application/octet-stream")


def _content_block(data: bytes, ext: str) -> dict:
    """Wrap the document as a document or image block, per its type."""
    ext = (ext or "").lower()
    b64 = base64.standard_b64encode(data).decode("ascii")
    if ext == ".pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": b64}}
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return {"type": "image",
                "source": {"type": "base64", "media_type": _media_type(ext),
                           "data": b64}}
    # HTML and anything else: send as text. A factsheet page is prose,
    # and the model reads it as well as it reads a PDF.
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return {"type": "text", "text": text[:400_000]}


def _estimate_cost(model: str, usage: dict) -> float | None:
    price = MODEL_PRICES.get(model)
    if not price or not isinstance(usage, dict):
        return None
    inp = float(usage.get("input_tokens") or 0)
    out = float(usage.get("output_tokens") or 0)
    return round(inp / 1e6 * price[0] + out / 1e6 * price[1], 4)


def _parse_json_response(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    The prompt asks for bare JSON, and the model complies almost always —
    but "almost" is not a contract, so a fenced or prose-wrapped reply is
    recovered rather than failed.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            pass
    raise ValueError("the model did not return usable JSON")


def extract_from_document(data: bytes, ext: str, *,
                          model: str = DEFAULT_MODEL,
                          timeout: int = 180,
                          prompt: str | None = None) -> dict:
    """Send a factsheet for extraction and return the parsed reply.

    Raises:
        RuntimeError: No API key, transport failure, or an API error.
        ValueError: The document is empty/too large, or the reply is not
            usable JSON.
    """
    import os
    import requests

    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise RuntimeError(
            f"no API key: set {API_KEY_ENV} in the environment and restart")
    if not data:
        raise ValueError("the document is empty")
    if len(data) > MAX_DOC_BYTES:
        raise ValueError(f"the document is {len(data)/1e6:.1f}MB; "
                         f"the limit is {MAX_DOC_BYTES/1e6:.0f}MB")

    payload = {
        "model": model,
        "max_tokens": 8000,
        # Deterministic: the same document should extract the same way
        # twice, or a Re-extract becomes a lottery rather than a retry.
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [_content_block(data, ext),
                        {"type": "text",
                         # A caller-supplied prompt overrides the generated
                         # one for this call only; nothing persists it.
                         "text": prompt or build_extraction_prompt()}],
        }],
    }
    try:
        resp = requests.post(
            ANTHROPIC_URL, json=payload, timeout=timeout,
            headers={"x-api-key": key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"})
    except requests.RequestException as exc:
        raise RuntimeError(f"could not reach the API: {exc}") from exc

    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message") or ""
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"API error {resp.status_code}: {detail}")

    body = resp.json()
    text = "".join(b.get("text") or "" for b in (body.get("content") or [])
                   if b.get("type") == "text")
    parsed = _parse_json_response(text)
    parsed["_usage"] = body.get("usage") or {}
    parsed["_model"] = model
    parsed["_cost_usd"] = _estimate_cost(model, body.get("usage") or {})
    return parsed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# Everything below runs on the model's reply before any of it is shown as
# a value. Nothing here trusts the model: a field is kept only if it is
# in the registry, carries a citation, declares a unit where one is
# required, and survives the same coercion a hand-typed value goes
# through.

_UNIT_SCALE = {
    "units": 1.0, "unit": 1.0, "": 1.0,
    "thousands": 1e3, "thousand": 1e3, "k": 1e3,
    "millions": 1e6, "million": 1e6, "m": 1e6, "mn": 1e6, "miljard": 1e9,
    "billions": 1e9, "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillions": 1e12, "trillion": 1e12,
}


def _to_number(v, *, decimal_comma_hint: bool = False):
    """Parse a number the model may have written in any locale.

    ``decimal_comma_hint`` resolves the one genuinely ambiguous case:
    "1,155" is 1155 with a thousands separator or 1.155 with a decimal
    comma, and nothing in the string says which. For a percent field the
    decimal reading is almost always right — a 1155% turnover is not a
    real figure, and reading 1.155% as 1155% silently rejects it as out
    of range, which is how the field disappeared.
    """
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str):
        return None
    t = v.strip().replace("%", "").replace("\u00a0", " ").replace(" ", "")
    if not t:
        return None
    # "84.138,91" is European; "84,138.91" is Anglo. Whichever separator
    # comes LAST is the decimal point.
    if "," in t and "." in t:
        t = (t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".")
             else t.replace(",", ""))
    elif t.count(".") > 1:
        # "8.565.668.400" — more than one dot can only be a thousands
        # separator, since a number has at most one decimal point.
        t = t.replace(".", "")
    elif "." in t and len(t.split(".")[-1]) == 3 and not decimal_comma_hint:
        # A single dot with exactly three trailing digits is ambiguous.
        # "8.565" is 8565 in a European document and 8.565 in an Anglo
        # one; for a currency amount the thousands reading is right, and
        # the hint marks the percent fields where it is not.
        t = t.replace(".", "")
    elif "," in t:
        tail = t.split(",")[-1]
        if len(tail) != 3 or decimal_comma_hint:
            t = t.replace(",", ".")
        else:
            t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


# Words factsheets actually use, mapped onto the closed vocabularies.
# The prompt asks for the canonical value and mostly gets it; this is the
# safety net, because a near-miss is rejected outright and the field then
# vanishes with no sign that an answer was given.
_VOCAB_SYNONYMS: dict[str, dict[str, str]] = {
    "replication": {
        "physical": "full", "physicalreplication": "full",
        "fullreplication": "full", "fullphysical": "full",
        "directreplication": "full", "direct": "full", "inkind": "full",
        "replicating": "full",
        "optimised": "sampled", "optimized": "sampled",
        "sampling": "sampled", "representativesampling": "sampled",
        "stratifiedsampling": "sampled", "partial": "sampled",
        "swap": "synthetic", "swapbased": "synthetic",
        "unfundedswap": "synthetic", "fundedswap": "synthetic",
        "derivative": "synthetic", "derivatives": "synthetic",
    },
    "structure": {
        "exchangetradedfund": "etf", "ucitsetf": "etf",
        "sicav": "fund", "fcp": "fund", "oeic": "fund",
        "unittrust": "fund", "commoncontractualfund": "fund",
        "mutualfund": "fund", "indexfund": "fund", "beleggingsfonds": "fund",
        "openendfund": "fund", "investmenttrust": "fund",
    },
    "style": {
        "index": "passive", "indextracking": "passive", "tracker": "passive",
        "indexfonds": "passive", "indexed": "passive",
        "activelymanaged": "active", "stockpicking": "active",
    },
    "distribution": {
        "acc": "accumulating", "capitalisation": "accumulating",
        "capitalization": "accumulating", "accumuleren": "accumulating",
        "reinvested": "accumulating", "reinvesting": "accumulating",
        "thesaurierend": "accumulating",
        "dist": "distributing", "income": "distributing",
        "uitkerend": "distributing", "ausschuettend": "distributing",
        "payingdividends": "distributing",
    },
    # The bands as issuers actually label them. "Giant" and "mega" are
    # the top of a five-bucket Morningstar-style table and belong in
    # large; "micro" and "nano" are the bottom and belong in small. A
    # multi-cap or all-cap fund is describing a spread, which is mixed.
    "market_cap": {
        "giant": "large", "megacap": "large", "mega": "large",
        "largecap": "large", "big": "large",
        "midcap": "mid", "medium": "mid", "mediumcap": "mid",
        "middle": "mid",
        "smallcap": "small", "micro": "small", "microcap": "small",
        "nano": "small", "nanocap": "small", "smid": "small",
        "multicap": "mixed", "allcap": "mixed", "all": "mixed",
        "broad": "mixed", "diversified": "mixed", "blend": "mixed",
    },
    "primary_asset_class": {
        "equities": "equity", "shares": "equity", "stock": "equity",
        "stocks": "equity", "aandelen": "equity",
        "bond": "fixed_income", "bonds": "fixed_income",
        "fixedincome": "fixed_income", "debt": "fixed_income",
        "obligaties": "fixed_income",
        "multiasset": "mixed", "balanced": "mixed", "allocation": "mixed",
        "commodities": "commodity", "moneymarket": "cash",
    },
}


def _resolve_vocab_value(name: str, value: str, context: dict) -> str | None:
    """Map a near-miss onto a canonical key, or None if it cannot be.

    Case and spacing first — a model writing "Fixed Income" means
    ``fixed_income`` — then the app's own alias tables for the two fields
    whose vocabularies are data rather than constants.
    """
    v = value.strip()
    spec = OVERRIDABLE_FIELDS.get(name) or {}

    if spec.get("type") == "enum":
        vocab = field_vocab(spec)
        squashed = v.lower().replace(" ", "_").replace("-", "_")
        for allowed in vocab:
            if squashed == str(allowed).lower().replace(" ", "_"):
                return allowed
        # Semantic synonyms — "Physical" is not a spelling of "full", so
        # squashing cannot reach it.
        bare = re.sub(r"[^a-z]", "", v.lower())
        table = _VOCAB_SYNONYMS.get(name) or {}
        syn = table.get(bare)
        if syn is None:
            # Factsheets qualify these words — "Optimised sampling",
            # "Full physical replication" — so an exact-key lookup misses
            # the common phrasing. Longest match first, so "fullreplication"
            # is not shadowed by "full".
            for k in sorted(table, key=len, reverse=True):
                if k in bare:
                    syn = table[k]
                    break
        return syn if syn in vocab else None

    if name == "focus_detail":
        ftype = str((context or {}).get("focus_type") or "").strip().lower()
        # The type was renamed "region" -> "geography" in the registry
        # (LEGACY_FOCUS_TYPES records the rename) and this branch was
        # left testing the old name, so a geographic focus never
        # reached the alias table: every near-miss the table exists to
        # catch ("Europe", "Eurozone", "Emerging Markets") was rejected
        # instead, and the rejection printed the whole ~280-value
        # vocabulary as its reason. Both names are accepted - the model
        # is told "geography", and a value stored before the rename is
        # still worth resolving.
        try:
            if ftype in ("geography", "region"):
                from porxpy.resources import resolve_region_alias
                return resolve_region_alias(v)
            if ftype == "sector":
                from porxpy.resources import SECTOR_STYLE_ALIASES, _norm_phrase
                return SECTOR_STYLE_ALIASES.get(_norm_phrase(v))
        except Exception:
            return None
    return None


# Which model-reported holdings column feeds which row field. The four
# facet columns land in their RAW column, exactly as an upload's mapped
# columns and Yahoo's enrichment do (see upload.parse / _ENRICH_TARGET):
# the document's own wording is the evidence, and the node is derived
# from it on every read.
_HOLDINGS_FIELD_MAP: dict[str, str] = {
    "name":        "name",
    "ticker":      "ticker",
    "isin":        "isin",
    "cusip":       "cusip",
    "sector":      "sector_raw",
    "country":     "country_raw",
    "currency":    "currency_raw",
    "asset_class": "asset_raw",
    "maturity":    "maturity",
}
_HOLDINGS_NUMERIC_MAP: dict[str, str] = {
    "weight":   "weight_pct",
    "coupon":   "coupon",
    "duration": "duration",
}

# A factsheet lists a top-10, occasionally a full schedule of a few
# hundred. The cap is a guard against a runaway reply rather than a
# statement about real documents, and is reported when it bites.
MAX_HOLDINGS_ROWS = 600


def _validate_holdings(blk: dict, reject) -> dict:
    """Filter the model's position table down to storable rows.

    The same three guards the fields go through, in the terms a position
    table needs: a citation for the table, a name for every row, and a
    number for every weight. Facet columns are NOT resolved here — they
    are stored as the document worded them and resolved on read, which
    is what lets an alias added next month reach a row extracted today.

    Args:
        blk: The reply's ``holdings`` block.
        reject: The caller's rejection recorder.

    Returns:
        ``{rows, complete, sum_pct, page, quote}`` — or ``{}`` when the
        document stated no usable table.
    """
    if not isinstance(blk, dict):
        reject("holdings", "malformed entry")
        return {}
    quote = (blk.get("quote") or "").strip()
    if not quote:
        # The same rule the fields obey: a table nobody can point at in
        # the document is indistinguishable from a plausible invention,
        # and a fabricated position list is the most damaging thing this
        # pipeline could produce — every breakdown card can be told to
        # read from it.
        reject("holdings", "no supporting quote")
        return {}

    rows: list[dict] = []
    for it in (blk.get("rows") or []):
        if not isinstance(it, dict):
            continue
        row: dict = {}
        for src_key, field in _HOLDINGS_FIELD_MAP.items():
            v = it.get(src_key)
            if isinstance(v, str) and v.strip():
                row[field] = v.strip()
        name = (row.get("name") or "").strip()
        if not name:
            # A position with no name cannot be shown, matched to a
            # portfolio holding, or looked up — there is nothing to keep.
            continue
        for src_key, field in _HOLDINGS_NUMERIC_MAP.items():
            n = _to_number(it.get(src_key), decimal_comma_hint=True)
            if n is not None:
                row[field] = n
        rows.append(row)
        if len(rows) >= MAX_HOLDINGS_ROWS:
            reject("holdings",
                   f"only the first {MAX_HOLDINGS_ROWS} rows were kept")
            break

    if not rows:
        reject("holdings", "no usable rows")
        return {}

    total = round(sum(float(r.get("weight_pct") or 0.0) for r in rows), 4)
    return {
        "rows":     rows,
        # Trusted the same way a facet table's flag is: the arithmetic
        # wins over the claim, because a partial list reported as
        # complete misstates every position in it.
        "complete": bool(blk.get("complete")) and total >= 99.0,
        "claimed_complete": bool(blk.get("complete")),
        "sum_pct":  total,
        "page":     blk.get("page"),
        "quote":    quote[:400],
    }


def validate_extraction(raw: dict) -> dict:
    """Filter a model reply down to what may safely be offered.

    Returns:
        ``{as_of, fields, facets, holdings, rejected, usage, model,
        cost_usd}`` where ``fields`` maps a registry field to
        ``{value, page, quote, confidence, unit}``, ``facets`` maps a
        facet to ``{items, sum_pct, complete, page, quote}``, and
        ``holdings`` is ``{rows, complete, sum_pct, page, quote}`` or
        ``{}``. ``rejected`` lists what was dropped and why, so a thin
        extraction is explicable rather than merely disappointing.
    """
    from porxpy.utils import coerce_override_value

    out_fields: dict[str, dict] = {}
    out_facets: dict[str, dict] = {}
    rejected: list[dict] = []

    # Plain field → value map, for the one registry rule that depends on
    # a sibling field: focus_detail's vocabulary is decided by
    # focus_type. The model's blocks are {value, quote, page, …}, and
    # handing those straight to the resolver made it try to .strip() a
    # dict. The resolver takes values, so give it values.
    raw_fields = (raw or {}).get("fields") or {}
    context = {
        name: blk.get("value")
        for name, blk in raw_fields.items()
        if isinstance(blk, dict)
    }

    def _reject(what, why):
        rejected.append({"item": what, "reason": why})

    # ---- fields ---------------------------------------------------------
    for name, blk in raw_fields.items():
        # The prompt names this field primary_asset_class, and the FACETS
        # section immediately below names a DIFFERENT thing asset_class —
        # so a reply that answers under the shorter, more natural name is
        # an entirely reasonable mistake to make, and rejecting it loses
        # a correct answer to a naming detail. Accept the alias.
        #
        # This also rescues extractions stored before the v0.47.0 rename,
        # which used the old name throughout.
        name = _FIELD_ALIASES.get(name, name)
        if name not in OVERRIDABLE_FIELDS or name in _SKIP_FIELDS:
            _reject(name, "not an overridable field")
            continue
        if not isinstance(blk, dict):
            _reject(name, "malformed entry")
            continue
        quote = (blk.get("quote") or "").strip()
        if not quote:
            # The whole guard against invention. A value with no quote is
            # indistinguishable from a plausible guess.
            _reject(name, "no supporting quote")
            continue

        spec = OVERRIDABLE_FIELDS[name]
        value = blk.get("value")
        if spec.get("type") == "number":
            num = _to_number(value, decimal_comma_hint=(spec.get("unit") == "%"))
            if num is None:
                _reject(name, f"not a number: {value!r}")
                continue
            unit = str(blk.get("unit") or "").strip().lower()
            if spec.get("unit") == "%":
                # A percent is a percent; the only sane conversion is
                # from a fraction, which the prompt does not ask for.
                num = num * 100.0 if unit in ("fraction", "ratio") else num
            else:
                scale = _UNIT_SCALE.get(unit)
                if scale is None:
                    _reject(name, f"unrecognised unit {unit!r}")
                    continue
                num = num * scale
            value = num

        # A closed vocabulary rejects a near-miss outright — "Europe" for
        # "europe", "Technology" for "technology" — and the field then
        # vanishes with no indication that an answer was given. Resolve
        # through the same alias tables the rest of the app uses before
        # giving up on it.
        if isinstance(value, str) and value.strip():
            value = _resolve_vocab_value(name, value, context) or value

        try:
            coerced = coerce_override_value(name, value, context=context)
        except ValueError as exc:
            _reject(name, str(exc))
            continue

        out_fields[name] = {
            "value":      coerced,
            "page":       blk.get("page"),
            "quote":      quote[:400],
            "confidence": (blk.get("confidence") or "").lower() or None,
            "unit":       blk.get("unit"),
        }

    # ---- facets ---------------------------------------------------------
    for facet, blk in ((raw or {}).get("facets") or {}).items():
        if facet not in BREAKDOWN_FACETS:
            _reject(f"facet:{facet}", "not a breakdown facet")
            continue
        if not isinstance(blk, dict):
            _reject(f"facet:{facet}", "malformed entry")
            continue
        items = []
        for it in (blk.get("items") or []):
            if not isinstance(it, dict):
                continue
            key = str(it.get("key") or "").strip()
            w = _to_number(it.get("weight"))
            if not key or w is None or w <= 0:
                continue
            items.append({"key": key, "weight": round(w, 6),
                          "label_in_document": str(it.get("label_in_document") or "")})
        if not items:
            _reject(f"facet:{facet}", "no usable rows")
            continue

        total = round(sum(i["weight"] for i in items), 4)
        # Trust the arithmetic over the model's own flag: a table summing
        # to 95.5 is partial whatever it claims, and a partial table
        # reported as complete misstates every row in it.
        complete = bool(blk.get("complete")) and total >= 99.0
        out_facets[facet] = {
            "items":    items,
            "sum_pct":  total,
            "complete": complete,
            "claimed_complete": bool(blk.get("complete")),
            "page":     blk.get("page"),
            "quote":    (blk.get("quote") or "").strip()[:400],
        }

    # ---- holdings -------------------------------------------------------
    out_holdings = _validate_holdings((raw or {}).get("holdings"), _reject) \
        if (raw or {}).get("holdings") is not None else {}

    # Identity, reported but never applied. A factsheet naming a
    # different ISIN usually means it is the wrong share class — worth
    # showing the user, never worth writing over the key every
    # fund-level record hangs on.
    ident_raw = (raw or {}).get("identity")
    identity = {}
    if isinstance(ident_raw, dict):
        for k in ("longName", "isin", "ticker", "exchange", "currency"):
            v = ident_raw.get(k)
            if isinstance(v, str) and v.strip():
                identity[k] = v.strip()

    as_of = (raw or {}).get("as_of")
    if isinstance(as_of, str):
        as_of = as_of.strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or ""):
            as_of = ""
    else:
        as_of = ""

    return {
        "as_of":     as_of,
        "identity":  identity,
        "fund_name": identity.get("longName") or (raw or {}).get("fund_name") or "",
        "isin":      (identity.get("isin")
                      or (raw or {}).get("isin") or "").strip().upper(),
        "fields":    out_fields,
        "facets":    out_facets,
        "holdings":  out_holdings,
        "rejected":  rejected,
        "usage":     (raw or {}).get("_usage") or {},
        "model":     (raw or {}).get("_model") or "",
        "cost_usd":  (raw or {}).get("_cost_usd"),
    }
