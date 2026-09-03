"""Export and import of pre-loaded fund sets and portfolio backups.

Building a usable pre-loaded set — 100–150 funds with holdings,
factsheets, corrected structure fields and per-facet source pins — is
days of work. A new install should be able to inherit that instead of
repeating it, and an existing install should be able to back it up.

Two bundles, deliberately separate:

* **Funds** (:func:`export_funds`) — the shared, curated asset. What a
  fund *is*, and what was established about it. Portable between
  installs and between users.
* **Portfolios** (:func:`export_portfolios`) — what *you* hold. Personal,
  not shareable, and restored on top of whatever fund set is present.

Mixing them would mean a user cannot take someone else's fund research
without also taking their holdings.

Both are plain zips with a readable ``manifest.json`` and JSON/CSV
inside. When an import goes wrong the user can open the file and see
why, which a pickled blob or a sqlite dump would not allow.

Import is TWO phase — :func:`inspect_bundle` then :func:`apply_bundle`.
The conflict decisions (skip / overwrite / merge / cancel) have to be
made against a list of what actually collides, and that list can only be
produced by reading the bundle against the current install. A one-shot
import would either overwrite silently or refuse on the first clash.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from porxpy import VERSION
# Bundles are disk work rather than network work, but a large
# cache makes them a multi-second wait all the same, and the rule
# is the same: a wait says what it is waiting for.
from porxpy.utils import progress_phase, progress_update
from porxpy.config import (BREAKDOWN_FACETS, FACTSHEETS_DIR, FUNDS_DIR,
                           LISTINGS_DIR, OVERRIDES_FP, PORTFOLIOS_FP,
                           SETTINGS_FP)

BUNDLE_FORMAT = 1

# Overrides that describe the FUND travel with it; overrides that
# describe how *you* use it do not.
#
# include_in_optimizer is a decision about your own portfolio
# construction, not a fact about the fund. Shipping it would have a new
# user's optimiser silently ignore funds because the exporter excluded
# them for reasons that do not apply.
#
# The breakdown_source pins DO travel: "this fund's factsheet beats
# Yahoo for sector" is curation, and re-deriving it is exactly the work
# a pre-loaded set exists to save.
#
# So do their two v0.77.0 siblings, for the same reason and by the same
# denylist-not-allowlist rule (a new registry field travels unless
# someone decides it is personal):
#   * breakdown_complete.<facet> — "no source can supply more for this
#     fund, read its coverage as the whole fund". A statement about the
#     fund and the material available for it, not about the importer's
#     portfolio. It must travel WITH its breakdown_source pin, since the
#     two together are what a card means.
#   * holdings_source — which of the three position lists this fund
#     shows. The lists themselves are in the fund cache and travel with
#     it, so shipping them without the choice would hand the importer
#     the curation and then discard the conclusion.
_PERSONAL_OVERRIDE_FIELDS: frozenset[str] = frozenset({"include_in_optimizer"})

# Price history is the bulk of a bundle and the most perishable thing in
# it — one refetch replaces it. Excluded unless asked for.
_BULKY_CATEGORIES: frozenset[str] = frozenset({"price_history"})

# Resource rows are matched on (type, name). Everything else in the row
# is either a key field or the alias column.
_RESOURCE_KEY_FIELDS = ("type", "name")

# A ``source_value`` is only worth exporting when it names somewhere the
# importer can also reach. An http(s) URL does — an issuer's holdings
# schedule or factsheet is public, and it is the most useful single thing
# in a curated bundle, because it is how the importer refreshes what they
# were given. A filesystem path does not: it is inert on any other
# machine, and it carries the exporter's username and directory layout
# out of their install with it.
#
# Three stores hold one, all under this same key name — the holdings blob
# (where the position list came from), ``upload_sources`` (what each
# upload dialog offers back), and pre-0.87.0 ``upload_prefs`` blobs still
# on disk. The scrub is therefore written against the key rather than
# against those three paths, so a fourth store adopting the convention is
# covered on the day it is written rather than the day someone remembers.
_SHAREABLE_SOURCE_PREFIXES = ("http://", "https://")


def _scrub_local_sources(blob: Any) -> Any:
    """Blank every non-URL ``source_value`` in a cache blob, in place.

    ``source_kind`` and ``filename`` are deliberately kept: they are what
    the holdings tile renders as ``LOCAL:<filename>``, which still tells
    the importer this list was supplied from a file rather than fetched
    from Yahoo. Only the location goes, because only the location is
    both useless to them and personal to the exporter.

    Args:
        blob: A parsed cache blob (or any part of one). Mutated in place
            — callers pass the freshly-parsed copy they are about to
            write into the zip, never anything shared.

    Returns:
        The same object, for use as an expression.
    """
    if isinstance(blob, dict):
        val = blob.get("source_value")
        if isinstance(val, str) and val.strip()                 and not val.strip().lower().startswith(_SHAREABLE_SOURCE_PREFIXES):
            blob["source_value"] = ""
        for v in blob.values():
            _scrub_local_sources(v)
    elif isinstance(blob, list):
        for v in blob:
            _scrub_local_sources(v)
    return blob


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(fp: Path) -> Any:
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _factsheet_isin(filename: str) -> str:
    """The ISIN a factsheet file belongs to, from its file name.

    Factsheets are stored as ``<ISIN>.<ext>`` plus an ``<ISIN>.json``
    sidecar (see ``utils._factsheet_paths``). The ``_``-split tolerates a
    hand-placed file carrying a suffix after the ISIN.

    Exists as one function because export and import both have to answer
    this question and must answer it identically. They did not: export
    took ``Path.stem`` and import took ``Path.name``, so import compared
    ``IE00B4L5Y983.PDF`` against a set of bare ISINs, matched nothing,
    and silently dropped every factsheet out of every fund bundle
    (fixed in 0.85.6).

    Args:
        filename: A factsheet file name or archive member name, with or
            without its extension.

    Returns:
        The upper-cased ISIN, or ``""`` when the name yields none.
    """
    return Path(filename).stem.split("_")[0].strip().upper()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_funds(isins: list[str] | None = None,
                 include_price_history: bool = False,
                 include_factsheets: bool = True,
                 progress_token: str = "") -> bytes:
    """Build a pre-loaded fund bundle.

    Args:
        isins: Which funds to export. ``None`` exports every fund with a
            cache file.
        include_price_history: Include the price series. Off by default:
            it dominates the file size and one refetch replaces it.
        include_factsheets: Include stored factsheet documents.

    Returns:
        The zip as bytes.

    Note:
        Remembered upload locations are scrubbed of filesystem paths on
        the way out — see :func:`_scrub_local_sources`. A bundle is meant
        to be handed to someone else, and a path from the exporter's
        machine is inert on theirs.
    """
    from porxpy.resources import _RESOURCE_FILES

    wanted = ({i.strip().upper() for i in isins if i and i.strip()}
              if isins is not None else None)

    buf = io.BytesIO()
    manifest: dict[str, Any] = {
        "format": BUNDLE_FORMAT, "kind": "funds",
        "app_version": VERSION, "exported_at": _now(),
        "include_price_history": bool(include_price_history),
        "funds": [], "listings": [], "resources": [], "factsheets": 0,
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # ── funds ────────────────────────────────────────────────────
        exported_isins: set[str] = set()
        if FUNDS_DIR.exists():
            _fund_files = sorted(FUNDS_DIR.glob("*.json"))
            progress_phase(progress_token, "packing funds", len(_fund_files))
            for _bi, fp in enumerate(_fund_files):
                progress_update(progress_token, _bi, len(_fund_files), 0.0)
                isin = fp.stem.upper()
                if wanted is not None and isin not in wanted:
                    continue
                blob = _read_json(fp)
                if not isinstance(blob, dict):
                    continue
                if not include_price_history:
                    blob = {k: v for k, v in blob.items()
                            if k not in _BULKY_CATEGORIES}
                z.writestr(f"funds/{isin}.json",
                           json.dumps(_scrub_local_sources(blob),
                                      indent=1, ensure_ascii=False))
                exported_isins.add(isin)
                manifest["funds"].append(
                    {"isin": isin,
                     "name": _fund_display_name(blob) or isin,
                     "categories": sorted(blob.keys())})
                # _fund_display_name reads the profile, which lives in
                # the LISTINGS file — so the name is filled in below,
                # once the listing for this ISIN has been seen.

        # ── listings (identity, keyed by ticker) ─────────────────────
        # Without these an imported fund has no ticker resolution and is
        # skipped by every path that starts from a ticker — the fund
        # would be present and unusable.
        if LISTINGS_DIR.exists():
            _lst_files = sorted(LISTINGS_DIR.glob("*.json"))
            progress_phase(progress_token, "packing listings", len(_lst_files))
            for _bi, fp in enumerate(_lst_files):
                progress_update(progress_token, _bi, len(_lst_files), 0.0)
                blob = _read_json(fp)
                if not isinstance(blob, dict):
                    continue
                ident = blob.get("identity") or {}
                isin = (ident.get("isin") or "").strip().upper()
                if not isin or (exported_isins and isin not in exported_isins):
                    continue
                # price_history and profile are LISTING categories, keyed
                # by ticker — not fund categories keyed by ISIN. The
                # bulky-category filter has to be applied here or
                # include_price_history does nothing at all, which is
                # what it did until this was run rather than reasoned
                # about.
                if not include_price_history:
                    blob = {k: v for k, v in blob.items()
                            if k not in _BULKY_CATEGORIES}
                z.writestr(f"listings/{fp.stem.upper()}.json",
                           json.dumps(_scrub_local_sources(blob),
                                      indent=1, ensure_ascii=False))
                manifest["listings"].append({"ticker": fp.stem.upper(),
                                             "isin": isin})
                # Fill the fund's display name from its profile, which
                # lives here rather than in the ISIN-keyed file.
                nm = _fund_display_name(blob)
                if nm:
                    for entry in manifest["funds"]:
                        if entry["isin"] == isin and entry["name"] == isin:
                            entry["name"] = nm

        # ── overrides, sliced to the exported funds ──────────────────
        ov = _read_json(OVERRIDES_FP) or {}
        sliced: dict[str, dict] = {}
        for key, entry in (ov.items() if isinstance(ov, dict) else []):
            isin = str(key).strip().upper()
            if exported_isins and isin not in exported_isins:
                continue
            if not isinstance(entry, dict):
                continue
            keep = {f: e for f, e in entry.items()
                    if f not in _PERSONAL_OVERRIDE_FIELDS}
            if keep:
                sliced[isin] = keep
        z.writestr("overrides.json",
                   json.dumps(sliced, indent=1, ensure_ascii=False))
        manifest["override_funds"] = len(sliced)

        # ── factsheets ───────────────────────────────────────────────
        if include_factsheets and FACTSHEETS_DIR.exists():
            progress_phase(progress_token, "packing factsheets", 0)
        for fp in sorted(FACTSHEETS_DIR.iterdir()):
                if not fp.is_file():
                    continue
                isin = _factsheet_isin(fp.name)
                if exported_isins and isin not in exported_isins:
                    continue
                z.write(fp, f"factsheets/{fp.name}")
                manifest["factsheets"] += 1

        # ── resource files ───────────────────────────────────────────
        for name, rfp in _RESOURCE_FILES:
            if rfp.exists():
                z.writestr(f"resources/{name}",
                           rfp.read_text(encoding="utf-8-sig"))
                manifest["resources"].append(name)

        z.writestr("manifest.json",
                   json.dumps(manifest, indent=1, ensure_ascii=False))

    return buf.getvalue()


def export_portfolios(progress_token: str = "") -> bytes:
    """Build a portfolio backup: portfolios, targets, cash and settings.

    Separate from the fund bundle because it is personal rather than
    shareable, and because a user restoring their own holdings should
    not be forced to also import somebody's fund research.
    """
    buf = io.BytesIO()
    manifest = {"format": BUNDLE_FORMAT, "kind": "portfolios",
                "app_version": VERSION, "exported_at": _now(),
                "portfolios": []}

    portfolios = _read_json(PORTFOLIOS_FP)
    portfolios = portfolios if isinstance(portfolios, list) else []
    for p in portfolios:
        if isinstance(p, dict):
            manifest["portfolios"].append(
                {"id": p.get("id"), "name": p.get("name"),
                 "funds": len(p.get("funds") or [])})

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("portfolios.json",
                   json.dumps(portfolios, indent=1, ensure_ascii=False))
        settings = _read_json(SETTINGS_FP)
        if settings is not None:
            z.writestr("settings.json",
                       json.dumps(settings, indent=1, ensure_ascii=False))
        z.writestr("manifest.json",
                   json.dumps(manifest, indent=1, ensure_ascii=False))
    return buf.getvalue()


def _fund_display_name(blob: dict) -> str:
    prof = (blob.get("profile") or {}).get("value") or {}
    if isinstance(prof, dict):
        return (prof.get("longName") or prof.get("shortName") or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Import — phase 1: inspect
# ---------------------------------------------------------------------------


def inspect_bundle(data: bytes) -> dict:
    """Read a bundle and report what it holds and what it would collide with.

    Nothing is written. The caller shows the conflicts, collects a
    decision per fund (or one decision for all), and calls
    :func:`apply_bundle`.

    Returns:
        ``{kind, manifest, funds: [...], resources: [...], errors: [...]}``
        where each fund carries ``exists`` and, when it does, the count
        of local overrides an overwrite would replace — so "overwrite"
        can state its cost at the moment of choosing rather than
        discarding work silently.
    """
    out: dict[str, Any] = {"kind": "", "manifest": {}, "funds": [],
                           "resources": [], "portfolios": [], "errors": []}
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        out["errors"].append(f"not a readable zip: {exc}")
        return out

    with z:
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except Exception:
            out["errors"].append("manifest.json missing or unreadable — this "
                                 "does not look like a PorxPy bundle")
            return out
        out["manifest"] = manifest
        out["kind"] = (manifest.get("kind") or "").strip()

        if manifest.get("format") != BUNDLE_FORMAT:
            out["errors"].append(
                f"bundle format {manifest.get('format')!r} is not "
                f"{BUNDLE_FORMAT} — this bundle was written by a different "
                f"version and cannot be read safely")
            return out

        if out["kind"] == "portfolios":
            local = _read_json(PORTFOLIOS_FP)
            local_ids = {p.get("id") for p in (local or [])
                         if isinstance(p, dict)}
            for p in manifest.get("portfolios") or []:
                out["portfolios"].append({**p,
                                          "exists": p.get("id") in local_ids})
            # Which funds the backup references but this install lacks.
            # Reported, never blocking: a portfolio backup should not be
            # refused because of an unrelated gap in the fund set.
            missing = set()
            try:
                ps = json.loads(z.read("portfolios.json").decode("utf-8"))
            except Exception:
                ps = []
            for p in ps if isinstance(ps, list) else []:
                for f in (p.get("funds") or []):
                    isin = (f.get("isin") or "").strip().upper()
                    if isin and not (FUNDS_DIR / f"{isin}.json").exists():
                        missing.add(f"{f.get('ticker') or isin} ({isin})")
            out["missing_funds"] = sorted(missing)
            return out

        ov_local = _read_json(OVERRIDES_FP) or {}
        ov_bundle = {}
        if "overrides.json" in z.namelist():
            try:
                ov_bundle = json.loads(z.read("overrides.json").decode("utf-8"))
            except Exception:
                ov_bundle = {}

        for entry in manifest.get("funds") or []:
            isin = (entry.get("isin") or "").strip().upper()
            if not isin:
                continue
            exists = (FUNDS_DIR / f"{isin}.json").exists()
            local_ov = (ov_local.get(isin) or {}) if exists else {}
            out["funds"].append({
                "isin": isin,
                "name": entry.get("name") or isin,
                "categories": entry.get("categories") or [],
                "exists": exists,
                # Counted so the prompt can say "overwrite (replaces 3 of
                # your edits)" rather than leaving the user to discover
                # the loss afterwards.
                "local_overrides": len([f for f in local_ov
                                        if f not in _PERSONAL_OVERRIDE_FIELDS]),
                "bundle_overrides": len(ov_bundle.get(isin) or {}),
            })

        for name in manifest.get("resources") or []:
            try:
                rows = _parse_resource_bytes(z.read(f"resources/{name}"))
            except Exception:
                continue
            out["resources"].append({"file": name,
                                     **_resource_conflicts(name, rows)})
    return out


def _parse_resource_bytes(raw: bytes) -> list[dict]:
    """Parse a resource CSV out of a bundle, tolerantly."""
    import csv

    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    lines = text.splitlines()
    if lines and lines[0].strip().lower().startswith("version="):
        lines = lines[1:]                       # pre-0.64.0 bundle
    delim = ";" if (lines and lines[0].count(";") > lines[0].count(",")) else ","
    return list(csv.DictReader(lines, delimiter=delim))


def _resource_conflicts(name: str, bundle_rows: list[dict]) -> dict:
    """Which rows are new, identical, or differ from the local file."""
    from porxpy.config import RESOURCES_DIR
    from porxpy.resources import parse_resource_csv

    fp = RESOURCES_DIR / name
    local = parse_resource_csv(fp) if fp.exists() else []
    local_by_key = {_row_key(r): r for r in local}

    new_rows, conflicts, identical = [], [], 0
    for r in bundle_rows:
        k = _row_key(r)
        if not k[1]:
            continue
        cur = local_by_key.get(k)
        if cur is None:
            new_rows.append({"type": k[0], "name": k[1]})
        elif _rows_equal(cur, r):
            identical += 1
        else:
            conflicts.append({
                "type": k[0], "name": k[1],
                "differs": sorted(
                    f for f in set(cur) | set(r)
                    if (cur.get(f) or "").strip() != (r.get(f) or "").strip()),
                "local_aliases":  _alias_set(cur),
                "bundle_aliases": _alias_set(r),
            })
    return {"new": new_rows, "conflicts": conflicts, "identical": identical}


def _row_key(row: dict) -> tuple[str, str]:
    return ((row.get("type") or "").strip().lower(),
            (row.get("name") or "").strip().lower())


def _rows_equal(a: dict, b: dict) -> bool:
    fields = set(a) | set(b)
    return all((a.get(f) or "").strip() == (b.get(f) or "").strip()
               for f in fields)


def _alias_set(row: dict) -> list[str]:
    return sorted({m.strip() for m in (row.get("matches") or "").split("|")
                   if m.strip()})


# ---------------------------------------------------------------------------
# Import — phase 2: apply
# ---------------------------------------------------------------------------


def apply_bundle(data: bytes, fund_actions: dict[str, str] | None = None,
                 default_fund_action: str = "skip",
                 resource_action: str = "merge_aliases",
                 progress_token: str = "") -> dict:
    """Write a bundle in, honouring the caller's conflict decisions.

    Args:
        data: The bundle bytes.
        fund_actions: ``{ISIN: "skip"|"overwrite"}`` for funds that
            already exist locally. A fund not present locally is always
            imported — there is nothing to decide.
        default_fund_action: What to do with a conflicting fund not named
            in ``fund_actions``. This is the "apply to all" answer.
        resource_action: ``"skip"`` | ``"overwrite"`` | ``"merge_aliases"``.

            ``merge_aliases`` unions the ``matches`` column and keeps
            every LOCAL key field. It exists because skip and overwrite
            both lose work on a resource row: two installs can define
            the same sector with different aliases, and whichever side
            loses has its aliases discarded. Merging is what is usually
            wanted and is the default.

    Returns:
        A report of what was written and skipped.
    """
    report: dict[str, Any] = {
        "funds_imported": [], "funds_skipped": [], "listings": 0,
        "factsheets": 0, "overrides": 0, "resources": [],
        "portfolios_imported": 0, "missing_funds": [], "errors": [],
    }
    fund_actions = {k.strip().upper(): v for k, v in (fund_actions or {}).items()}

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        report["errors"].append(f"not a readable zip: {exc}")
        return report

    with z:
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except Exception:
            report["errors"].append("manifest.json missing or unreadable")
            return report
        if manifest.get("format") != BUNDLE_FORMAT:
            report["errors"].append("unsupported bundle format")
            return report

        if (manifest.get("kind") or "") == "portfolios":
            return _apply_portfolios(z, report)

        names = set(z.namelist())

        # ── funds ────────────────────────────────────────────────────
        accepted: set[str] = set()
        FUNDS_DIR.mkdir(parents=True, exist_ok=True)
        _entries = manifest.get("funds") or []
        progress_phase(progress_token, "writing funds", len(_entries))
        for _bi, entry in enumerate(_entries):
            progress_update(progress_token, _bi, len(_entries), 0.0)
            isin = (entry.get("isin") or "").strip().upper()
            arc  = f"funds/{isin}.json"
            if not isin or arc not in names:
                continue
            target = FUNDS_DIR / f"{isin}.json"
            if target.exists():
                action = fund_actions.get(isin, default_fund_action)
                if action != "overwrite":
                    report["funds_skipped"].append(isin)
                    continue
            try:
                incoming = json.loads(z.read(arc).decode("utf-8"))
            except Exception as exc:
                report["errors"].append(f"{isin}: unreadable ({exc})")
                continue
            # A bundle without price history must not wipe a local
            # series the user already has: the category is absent from
            # the bundle because it was not exported, which is not the
            # same as "this fund has none".
            if target.exists():
                local = _read_json(target) or {}
                for cat in _BULKY_CATEGORIES:
                    if cat in local and cat not in incoming:
                        incoming[cat] = local[cat]
            target.write_text(json.dumps(incoming, indent=1,
                                         ensure_ascii=False), encoding="utf-8")
            accepted.add(isin)
            report["funds_imported"].append(isin)

        # ── listings ─────────────────────────────────────────────────
        LISTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _arcs = sorted(n for n in names if n.startswith("listings/"))
        progress_phase(progress_token, "writing listings", len(_arcs))
        for _bi, arc in enumerate(_arcs):
            progress_update(progress_token, _bi, len(_arcs), 0.0)
            try:
                blob = json.loads(z.read(arc).decode("utf-8"))
            except Exception:
                continue
            isin = ((blob.get("identity") or {}).get("isin") or "").strip().upper()
            if isin and isin not in accepted:
                continue                    # its fund was skipped
            target = LISTINGS_DIR / Path(arc).name
            if target.exists():
                local = _read_json(target) or {}
                for cat in _BULKY_CATEGORIES:
                    if cat in local and cat not in blob:
                        blob[cat] = local[cat]
            target.write_text(json.dumps(blob, indent=1, ensure_ascii=False),
                              encoding="utf-8")
            report["listings"] += 1

        # ── overrides, for accepted funds only ───────────────────────
        if "overrides.json" in names and accepted:
            try:
                incoming_ov = json.loads(z.read("overrides.json").decode("utf-8"))
            except Exception:
                incoming_ov = {}
            local_ov = _read_json(OVERRIDES_FP) or {}
            if not isinstance(local_ov, dict):
                local_ov = {}
            for isin, entry in (incoming_ov or {}).items():
                isin = str(isin).strip().upper()
                if isin not in accepted or not isinstance(entry, dict):
                    continue
                # Wholesale, matching the fund-level decision: the user
                # chose overwrite for this fund knowing the count of
                # local edits it replaces. A field-level union would
                # make "overwrite" mean something other than overwrite.
                #
                # Personal fields are preserved regardless — they never
                # travelled, so the bundle cannot have an opinion.
                keep_personal = {f: e for f, e in (local_ov.get(isin) or {}).items()
                                 if f in _PERSONAL_OVERRIDE_FIELDS}
                merged = {f: e for f, e in entry.items()
                          if f not in _PERSONAL_OVERRIDE_FIELDS}
                merged.update(keep_personal)
                local_ov[isin] = merged
                report["overrides"] += 1
            OVERRIDES_FP.write_text(
                json.dumps(local_ov, indent=1, ensure_ascii=False),
                encoding="utf-8")

        # ── factsheets ───────────────────────────────────────────────
        FACTSHEETS_DIR.mkdir(parents=True, exist_ok=True)
        for arc in sorted(n for n in names if n.startswith("factsheets/")):
            fname = Path(arc).name
            if accepted and _factsheet_isin(fname) not in accepted:
                continue
            (FACTSHEETS_DIR / fname).write_bytes(z.read(arc))
            report["factsheets"] += 1

        # ── resource files ───────────────────────────────────────────
        if resource_action != "skip":
            for arc in sorted(n for n in names if n.startswith("resources/")):
                name = Path(arc).name
                try:
                    rows = _parse_resource_bytes(z.read(arc))
                except Exception:
                    continue
                report["resources"].append(
                    _merge_resource_file(name, rows, resource_action))

    _post_import_reload()
    return report


def _apply_portfolios(z: zipfile.ZipFile, report: dict) -> dict:
    """Restore a portfolio backup on top of the current install."""
    try:
        incoming = json.loads(z.read("portfolios.json").decode("utf-8"))
    except Exception as exc:
        report["errors"].append(f"portfolios.json unreadable: {exc}")
        return report
    if not isinstance(incoming, list):
        report["errors"].append("portfolios.json is not a list")
        return report

    local = _read_json(PORTFOLIOS_FP)
    local = local if isinstance(local, list) else []
    by_id = {p.get("id"): p for p in local if isinstance(p, dict)}
    for p in incoming:
        if isinstance(p, dict) and p.get("id"):
            by_id[p["id"]] = p
            report["portfolios_imported"] += 1
    PORTFOLIOS_FP.write_text(
        json.dumps(list(by_id.values()), indent=1, ensure_ascii=False),
        encoding="utf-8")

    # Funds the restored portfolios reference but this install lacks.
    # Listed, never fatal — a portfolio backup should not be refused
    # because of an unrelated gap in the fund set. The portfolio is
    # restored and the gap is something the user can close later.
    missing = set()
    for p in incoming:
        for f in (p.get("funds") or []) if isinstance(p, dict) else []:
            isin = (f.get("isin") or "").strip().upper()
            if isin and not (FUNDS_DIR / f"{isin}.json").exists():
                missing.add(f"{f.get('ticker') or isin} ({isin})")
    report["missing_funds"] = sorted(missing)

    if "settings.json" in z.namelist():
        try:
            SETTINGS_FP.write_text(
                z.read("settings.json").decode("utf-8"), encoding="utf-8")
        except Exception as exc:
            report["errors"].append(f"settings not restored: {exc}")
    return report


def _merge_resource_file(name: str, bundle_rows: list[dict],
                         action: str) -> dict:
    """Merge one resource file, returning what changed."""
    from porxpy.config import RESOURCES_DIR
    from porxpy.resources import parse_resource_csv, write_resource_csv

    fp = RESOURCES_DIR / name
    local = parse_resource_csv(fp) if fp.exists() else []
    fields = list(local[0].keys()) if local else (
        list(bundle_rows[0].keys()) if bundle_rows else [])
    if not fields:
        return {"file": name, "added": 0, "updated": 0, "skipped": 0}

    by_key = {_row_key(r): r for r in local}
    added = updated = skipped = 0

    for r in bundle_rows:
        k = _row_key(r)
        if not k[1]:
            continue
        cur = by_key.get(k)
        if cur is None:
            by_key[k] = {f: (r.get(f) or "") for f in fields}
            added += 1
            continue
        if _rows_equal(cur, r):
            continue
        if action == "overwrite":
            by_key[k] = {f: (r.get(f) or "") for f in fields}
            updated += 1
        elif action == "merge_aliases":
            # Key fields stay local; the alias columns become the union.
            # This is the option that loses nobody's work: two installs
            # naming the same sector with different spellings both keep
            # theirs, and the local file's description and parent win
            # because the local user is the one who has to live with it.
            merged = dict(cur)
            for col in ("matches", "style_match"):
                if col not in fields:
                    continue
                union = sorted({*_split(cur.get(col)), *_split(r.get(col))},
                               key=str.lower)
                merged[col] = "|".join(union)
            if not _rows_equal(merged, cur):
                by_key[k] = merged
                updated += 1
            else:
                skipped += 1
        else:
            skipped += 1

    write_resource_csv(fp, fields,
                       [{f: (row.get(f) or "") for f in fields}
                        for row in by_key.values()])
    return {"file": name, "added": added, "updated": updated,
            "skipped": skipped}


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def _post_import_reload() -> None:
    """Pick up everything an import just wrote, without a restart."""
    try:
        from porxpy.resources import reload_all_resources
        reload_all_resources()
    except Exception as exc:
        print(f"[Bundle] resource reload after import failed: {exc}")
