"""Portfolio overview as a PDF — the first of the Portfolio "Downloads".

Why this exists:
    Everything a portfolio overview needs is already computed for the
    screen — the valuation, the funds, the history, the targets, the
    X-ray — but it lives in five sub-tabs and one browser. A report is
    that same state on paper: something to keep, to send, to compare
    with next quarter's.

    This module LAYS OUT; it computes nothing about the portfolio. The
    route in ``app.py`` hands it the very payloads the screen is built
    from (``/view``, ``/price_history``, ``/api/scores``), so a number in
    the PDF and the same number on screen cannot disagree. The only
    arithmetic here is presentation: indexing the history, its moving
    averages, and picking the best and worst fund over the chosen period.

    A module of its own rather than a corner of an existing one, because
    no existing module owns document layout: ``bundles`` writes zips,
    ``upload`` reads spreadsheets, and neither has any business importing
    reportlab or matplotlib.

Rendering:
    reportlab lays out the pages, matplotlib draws the charts (through
    the object API and the Agg canvas, never ``pyplot``: the dev server
    is threaded, and pyplot's global figure state is not), and pypdf
    appends the funds' factsheets as real PDF pages. DejaVu Sans, which
    ships inside matplotlib, is registered for the text, so a fund name
    with characters outside Latin-1 prints rather than turning into
    boxes.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as _esc

from porxpy.config import (BREAKDOWN_FACETS, FACET_LEVELS, META_FACETS,
                           TARGET_FACETS)

# ---------------------------------------------------------------------------
# Vocabulary on paper
# ---------------------------------------------------------------------------
FACET_TITLES = {
    "asset_class": "Asset class", "sector": "Sector", "country": "Country",
    "currency": "Currency", "market_cap": "Market cap",
    "style_box": "Equity style", "focus_theme": "Thematic focus",
}
LEVEL_TITLES = {
    "sub_sector": "sub sector", "sector": "sector", "super_sector": "super sector",
    "country": "country", "region": "region", "super_region": "developed/emerging",
    "sub_class": "sub class", "asset_class": "asset class",
    "super_class": "super class", "currency": "currency",
}

# The app's own chart palette (SCOL in fund_explorer.html), so a sector
# is drawn in a recognisably related way on paper and on screen.
PALETTE = ["#4f8ef7", "#7b61ff", "#34d399", "#f59e0b", "#f87171", "#a78bfa",
           "#38bdf8", "#fb923c", "#4ade80", "#e879f9", "#facc15", "#94a3b8"]
RESIDUAL_COLOUR = {"unknown": "#9ca3af", "n/a": "#d1d5db"}

# "ytd" has no fixed length: it runs from 1 January of the latest date's
# year, so history_series handles it by name (v0.142.0).
PERIODS = {"ytd": None, "1y": 365, "3y": 3 * 365, "5y": 5 * 365, "max": None}
PERIOD_TITLES = {"ytd": "year to date", "1y": "1 year", "3y": "3 years",
                 "5y": "5 years", "max": "all history"}


def middle_level(facet: str) -> str:
    """The middle node of a facet's tree: sector, region, asset class.

    Levels are listed finest first, so the middle of three is index 1. A
    one-level facet (currency, the metadata facets) has only its own.
    """
    levels = FACET_LEVELS.get(facet) or (facet,)
    return levels[len(levels) // 2] if len(levels) >= 3 else levels[-1]


def _humanise(key: str) -> str:
    """"europeDeveloped" → "Europe Developed"; "consumer cyclical" →
    "Consumer Cyclical"."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(key or "")).replace("_", " ")
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def bucket_label(facet: str, key: str, labels: dict) -> str:
    """A bucket key as a reader expects it.

    The vocabulary's own label wins where it says more than the key
    (``labels`` is the route's ``{facet: {key: label}}`` from the
    resource files); residual keys and currencies have fixed spellings;
    everything else is humanised.
    """
    if key == "unknown":
        return "Unknown"
    if key == "n/a":
        return "Not applicable"
    if facet == "currency":
        return str(key).upper()
    lbl = (labels.get(facet) or {}).get(key)
    if lbl and lbl != key:
        return lbl
    return _humanise(key)


# ---------------------------------------------------------------------------
# Fonts and styles
# ---------------------------------------------------------------------------
_FONTS_READY = False


def _fonts() -> tuple[str, str]:
    """Register DejaVu Sans from matplotlib's own data, once."""
    global _FONTS_READY
    if not _FONTS_READY:
        import matplotlib
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        pdfmetrics.registerFont(TTFont("DejaVu", str(base / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(base / "DejaVuSans-Bold.ttf")))
        # As a family, or <b> inside a Paragraph has no bold face to
        # switch to and silently stays regular. No italic is shipped, so
        # <i> maps to the regular face rather than failing.
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold",
                                      italic="DejaVu", boldItalic="DejaVu-Bold")
        _FONTS_READY = True
    return "DejaVu", "DejaVu-Bold"


def _styles():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    reg, bold = _fonts()
    return {
        "title": ParagraphStyle("t", fontName=bold, fontSize=18, leading=22,
                                spaceAfter=2),
        "sub": ParagraphStyle("s", fontName=reg, fontSize=9, leading=12,
                              textColor=colors.HexColor("#6b7280"), spaceAfter=10),
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=13, leading=16,
                             spaceBefore=10, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=10, leading=13,
                             spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("b", fontName=reg, fontSize=8.5, leading=11.5),
        "note": ParagraphStyle("n", fontName=reg, fontSize=7.5, leading=10,
                               textColor=colors.HexColor("#6b7280"), spaceBefore=3),
        "cell": ParagraphStyle("c", fontName=reg, fontSize=7.5, leading=9.5),
        "cellb": ParagraphStyle("cb", fontName=bold, fontSize=7.5, leading=9.5),
        # A wrapping header cell, right-aligned like the numbers below it.
        "cellsm": ParagraphStyle("cs", fontName=reg, fontSize=7, leading=8.6),
        "hcellL": ParagraphStyle("hcl", fontName=bold, fontSize=7, leading=8.4,
                                 textColor=colors.HexColor("#374151")),
        "hcell": ParagraphStyle("hc", fontName=bold, fontSize=7, leading=8.4,
                                alignment=2, textColor=colors.HexColor("#374151")),
    }


def _table(rows, widths, *, header=True, align_right_from=1, zebra=True,
           extra=None, header_rows=1, size=7.5):
    """One table look for every table in the report.

    ``header_rows`` > 1 is a grouped header (the funds table's "Returns"
    and "Rank" bands over their columns): every header row is styled
    and repeated on a page break, and the zebra starts below them.
    """
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    reg, bold = _fonts()
    hr = header_rows if header else 0
    t = Table(rows, colWidths=widths, repeatRows=hr)
    st = [
        ("FONT", (0, 0), (-1, -1), reg, size),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
    ]
    if align_right_from is not None:
        st.append(("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"))
    if header:
        st += [("FONT", (0, 0), (-1, hr - 1), bold, size),
               ("TEXTCOLOR", (0, 0), (-1, hr - 1), colors.HexColor("#374151")),
               ("LINEBELOW", (0, hr - 1), (-1, hr - 1), 0.6, colors.HexColor("#9ca3af"))]
    if zebra:
        for i in range(hr, len(rows)):
            if (i - hr) % 2 == 1:
                st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f3f4f6")))
    t.setStyle(TableStyle(st + (extra or [])))
    return t


def _money(v, cur: str) -> str:
    if v is None:
        return "—"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(cur, "")
    s = f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"
    return f"{sym}{s}" if sym else f"{s} {cur}"


def _fund_size(v, cur) -> str:
    """A fund's assets as the screen writes them (``fmtFundSize``)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "\u2014"
    pre = f"{(cur or '').upper()} " if cur else ""
    if abs(n) >= 1e12:
        return f"{pre}{n / 1e12:.2f}T"
    if abs(n) >= 1e9:
        return f"{pre}{n / 1e9:.2f}B"
    return f"{pre}{n / 1e6:.2f}M"


def _pct(v, digits=1, signed=False) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


# ---------------------------------------------------------------------------
# Charts (matplotlib object API, Agg — thread-safe)
# ---------------------------------------------------------------------------
def _fig(w_in: float, h_in: float):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(w_in, h_in), dpi=200)
    FigureCanvasAgg(fig)
    return fig


def _fig_image(fig, width_pt: float):
    """A matplotlib figure as a reportlab Image of the given width."""
    from reportlab.platypus import Image
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    w, h = fig.get_size_inches()
    return Image(buf, width=width_pt, height=width_pt * h / w)


def _style_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color("#9ca3af")
    ax.spines["bottom"].set_color("#9ca3af")
    ax.tick_params(colors="#4b5563", labelsize=7)


def _moving_average(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    acc = 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        out.append(acc / n if i >= n - 1 else None)
    return out


def history_series(ph: dict, period: str, include_cash: bool) -> dict:
    """The indexed portfolio history for a period, with its averages and
    the best and worst fund over the same window.

    The averages are taken over the FULL history and then cut to the
    window, so a 200-day line is real from the first day shown rather
    than starting 200 days in. Best and worst are judged only among funds
    that existed at the window's start: the history back-fills a fund's
    first price before its inception, so a younger fund would otherwise
    "return" nothing over the days it did not exist.
    """
    dates = [date.fromisoformat(d) for d in (ph.get("dates") or [])]
    vals = list(ph.get("portfolio_values" if include_cash
                       else "portfolio_values_ex_cash") or [])
    if not dates or not vals:
        return {}
    days = PERIODS.get(period)
    start = 0
    if period == "ytd":
        # From the first close of the latest date's year — the fund page's
        # YTD figure starts from the same point.
        jan1 = date(dates[-1].year, 1, 1)
        start = next((i for i, d in enumerate(dates) if d >= jan1), len(dates) - 1)
    elif days:
        cutoff = dates[-1] - timedelta(days=days)
        start = next((i for i, d in enumerate(dates) if d >= cutoff), 0)
    base = vals[start] or 1.0
    idx = lambda xs: [x / base * 100 if x is not None else None for x in xs[start:]]
    ma50, ma200 = _moving_average(vals, 50), _moving_average(vals, 200)

    ranked = []
    for f in ph.get("funds") or []:
        if f.get("is_cash"):
            continue
        fv = f.get("values") or []
        inc = f.get("inception_date")
        if len(fv) != len(dates) or not fv[start]:
            continue
        if inc and date.fromisoformat(inc) > dates[start]:
            continue
        ranked.append((fv[-1] / fv[start] - 1, f))
    ranked.sort(key=lambda x: x[0])
    pick = lambda r: ({"ticker": r[1].get("ticker"), "name": r[1].get("name"),
                       "return": r[0] * 100,
                       "values": [v / r[1]["values"][start] * 100
                                  for v in r[1]["values"][start:]]}
                      if r else None)
    return {
        "dates": dates[start:], "portfolio": idx(vals), "ma50": idx(ma50),
        "ma200": idx(ma200), "return": (vals[-1] / base - 1) * 100,
        "best": pick(ranked[-1]) if ranked else None,
        "worst": pick(ranked[0]) if len(ranked) > 1 else None,
        "excluded_young": sum(
            1 for f in ph.get("funds") or [] if not f.get("is_cash")
            and f.get("inception_date")
            and date.fromisoformat(f["inception_date"]) > dates[start]),
    }


def _history_chart(h: dict, width_pt: float):
    fig = _fig(7.2, 3.1)
    ax = fig.add_subplot(111)
    _style_axes(ax)
    d = h["dates"]
    ax.plot(d, h["portfolio"], color="#1f2937", lw=1.4, label="Portfolio")
    ax.plot(d, h["ma50"], color="#4f8ef7", lw=0.9, ls="--", label="50-day average")
    ax.plot(d, h["ma200"], color="#f59e0b", lw=0.9, ls="--", label="200-day average")
    if h.get("best"):
        ax.plot(d, h["best"]["values"], color="#16a34a", lw=0.9, alpha=.85,
                label=f"Best: {h['best']['ticker']} ({h['best']['return']:+.1f}%)")
    if h.get("worst"):
        ax.plot(d, h["worst"]["values"], color="#dc2626", lw=0.9, alpha=.85,
                label=f"Worst: {h['worst']['ticker']} ({h['worst']['return']:+.1f}%)")
    ax.axhline(100, color="#d1d5db", lw=0.6)
    ax.set_ylabel("Index (start = 100)", fontsize=7, color="#4b5563")
    ax.legend(fontsize=6.5, frameon=False, loc="upper left", ncol=2)
    ax.grid(axis="y", color="#eef0f3", lw=0.6)
    fig.autofmt_xdate(rotation=0, ha="center")
    return _fig_image(fig, width_pt)


def _xray_chart(facet: str, items: list[dict], labels: dict, width_pt: float,
                top: int = 10):
    """One facet at its middle level as horizontal bars, largest first.

    The tail beyond ``top`` folds into one "Other" bar; unknown and n/a
    stay their own bars, in grey, because they are not part of the tail
    but a statement about the data.
    """
    real = [i for i in items if i["key"] not in ("unknown", "n/a")]
    resid = [i for i in items if i["key"] in ("unknown", "n/a")]
    real.sort(key=lambda i: -i["weight"])
    shown = real[:top]
    rest = sum(i["weight"] for i in real[top:])
    bars = [(bucket_label(facet, i["key"], labels), i["weight"], PALETTE[n % len(PALETTE)])
            for n, i in enumerate(shown)]
    if rest > 0:
        bars.append((f"Other ({len(real) - top})", rest, "#cbd5e1"))
    bars += [(bucket_label(facet, i["key"], labels), i["weight"],
              RESIDUAL_COLOUR[i["key"]]) for i in resid if i["weight"] > 0]
    n = max(len(bars), 1)
    fig = _fig(3.5, 0.32 + 0.2 * n)
    ax = fig.add_subplot(111)
    _style_axes(ax)
    ys = list(range(len(bars)))[::-1]
    ax.barh(ys, [b[1] * 100 for b in bars], color=[b[2] for b in bars], height=0.7)
    ax.set_yticks(ys, [b[0][:28] for b in bars], fontsize=6.5)
    for y, b in zip(ys, bars):
        ax.text(b[1] * 100, y, f" {b[1] * 100:.1f}%", va="center", fontsize=6,
                color="#374151")
    ax.set_xlim(0, max([b[1] * 100 for b in bars] + [1]) * 1.25)
    ax.set_xticks([])
    ax.spines["bottom"].set_visible(False)
    return _fig_image(fig, width_pt)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def build_portfolio_report(view: dict, ph: dict, score_models: list[dict], *,
                           period: str = "1y", include_cash: bool = True,
                           labels: dict | None = None,
                           factsheets: list[dict] | None = None,
                           app_version: str = "",
                           say: Callable[[str], None] | None = None,
                           check: Callable[[], None] | None = None) -> bytes:
    """Lay out the portfolio overview and return the PDF bytes.

    Args:
        view: ``/api/portfolios/<pid>/view``'s payload.
        ph: ``/api/portfolios/<pid>/price_history``'s payload.
        score_models: ``/api/scores``'s payload once per weight model, in
            the order the Rank columns show them (cost driven, cost and
            returns, returns driven). The returns and the peer-group size
            are read off the first; they do not depend on the weights.
        period: A key of :data:`PERIODS`, for the history chart.
        include_cash: Count the cash held by the user in the history and
            the X-ray — the X-ray's own "include cash" question. Targets
            are always on the fund side, as everywhere else.
        labels: ``{facet: {key: label}}`` from the resource files.
        factsheets: ``[{ticker, name, path, ext}]`` to append, in order.
        app_version: Printed in the footer.
        say: Progress phase callback. ``check``: raises to stop.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether,
                                    NextPageTemplate, PageBreak, PageTemplate,
                                    Paragraph, Spacer, Table)
    say = say or (lambda _m: None)
    check = check or (lambda: None)
    labels = labels or {}
    S = _styles()
    reg, bold = _fonts()

    p = view.get("portfolio") or {}
    name = p.get("name") or "Portfolio"
    cur = (view.get("base_currency") or p.get("base_currency") or "EUR").upper()
    total = view.get("total_value_base") or 0.0
    funds_total = view.get("funds_value_base") or 0.0
    cash_total = view.get("cash_value_base") or 0.0
    page_w = A4[0] - 30 * mm
    stamp = datetime.now().strftime("%d %B %Y, %H:%M")
    score_models = [m for m in (score_models or []) if m]
    sc = (score_models[0].get("scores") if score_models else None) or {}

    story: list = []
    story.append(Paragraph(f"Portfolio overview — {_esc(name)}", S["title"]))
    story.append(Paragraph(
        f"{stamp} · values in {cur}", S["sub"]))

    # ── 1. Summary ───────────────────────────────────────────────────────
    say("laying out the summary")
    funds = [f for f in view.get("funds") or [] if not f.get("is_cash")]
    cash = [f for f in view.get("funds") or [] if f.get("is_cash")]
    story.append(Paragraph("1. Summary", S["h1"]))
    sum_rows = [["", f"Value ({cur})", "Share"],
                ["Total portfolio value", _money(total, cur), "100.0%"],
                [f"Funds ({len(funds)})", _money(funds_total, cur),
                 _pct(funds_total / total * 100 if total else None)],
                [f"Cash positions ({len(cash)})", _money(cash_total, cur),
                 _pct(cash_total / total * 100 if total else None)]]
    reserve = view.get("cash_reserve")
    if reserve:
        sum_rows.append(["  of which held back (cash held by me)",
                         _money(reserve, cur), _pct(reserve / total * 100 if total else None)])
    story.append(_table(sum_rows, [page_w * .55, page_w * .25, page_w * .2]))

    # Funds aggregated by their primary asset class — the one-word
    # classification of each fund, not the look-through.
    by_ac: dict[str, list[float]] = {}
    for f in funds:
        v = (f.get("valuation") or {}).get("value_base") or 0.0
        k = f.get("effective_asset_class") or "unknown"
        by_ac.setdefault(k, [0, 0.0])
        by_ac[k][0] += 1
        by_ac[k][1] += v
    story.append(Paragraph("Funds by primary asset class", S["h2"]))
    rows = [["Asset class", "Funds", f"Value ({cur})", "Share of funds"]]
    for k, (n, v) in sorted(by_ac.items(), key=lambda kv: -kv[1][1]):
        rows.append([_humanise(k), str(n), _money(v, cur),
                     _pct(v / funds_total * 100 if funds_total else None)])
    story.append(_table(rows, [page_w * .4, page_w * .15, page_w * .25, page_w * .2]))

    if cash:
        story.append(Paragraph("Cash positions", S["h2"]))
        rows = [["Account", "Currency", "Amount", f"Value ({cur})"]]
        for c in cash:
            val = c.get("valuation") or {}
            ccy = (val.get("native_currency") or "").upper()
            rows.append([Paragraph(_esc(c.get("name") or c.get("ticker") or ""), S["cell"]),
                         ccy, _money(val.get("value_native"), ccy),
                         _money(val.get("value_base"), cur)])
        story.append(_table(rows, [page_w * .45, page_w * .13, page_w * .21, page_w * .21]))
    check()

    # ── 2. Funds ─────────────────────────────────────────────────────────
    say("laying out the funds")
    # On a landscape page of its own: sixteen columns of figures do not
    # fit across portrait A4 at a readable size, and every other section
    # is drawn for portrait, so only this one turns.
    land_w = A4[1] - 30 * mm
    story.append(NextPageTemplate("landscape"))
    story.append(PageBreak())
    story.append(Paragraph("2. Funds in the portfolio", S["h1"]))
    # The columns the user asked for: value and weight, the trailing
    # returns, cost and size as FIGURES, the peer group's size, then the
    # fund's rank - overall / among its peers - under each weight model.
    # The ranks are the screen's scores, one /api/scores payload per
    # model, not recomputed here.
    ret_keys = [("1m", "1M"), ("3m", "3M"), ("1y", "1Y"), ("3y", "3Y")]
    # Short names for the built-in models so three columns fit beside
    # the figures; a model the user added keeps its own label.
    short = {"cost_driven": "Cost", "cost_and_returns": "Cost + returns",
             "returns_driven": "Returns"}
    rank_cols = [(short.get(m.get("preset")) or m.get("label") or m.get("preset") or "?",
                  m.get("scores") or {}) for m in score_models]
    n_fixed = 4                      # Fund, Asset class, Value, Weight
    top = (["", "", "", ""] + ["Returns", "", "", ""] + ["", "", ""]
           + ["Rank \u2014 overall / peer"] + [""] * (len(rank_cols) - 1))
    sub = (["Fund", Paragraph("Asset class", S["hcellL"]), "Value", "Weight"] + [t for _k, t in ret_keys]
           + ["TER", "Size", "Peers"]
           + [Paragraph(lbl, S["hcell"]) for lbl, _s in rank_cols])
    rows = [top, sub]

    def _rank(sd: dict) -> str:
        sa, sp = sd.get("score_all"), sd.get("score_peer")
        if sa is None and sp is None:
            return "\u2014"
        return (("\u2014" if sa is None else f"{sa:.0f}") + " / "
                + ("\u2014" if sp is None else f"{sp:.0f}"))

    for f in sorted(funds, key=lambda f: -((f.get("valuation") or {}).get("value_base") or 0)):
        tk = f.get("ticker") or ""
        prof = ((f.get("data") or {}).get("profile") or {})
        nm = prof.get("longName") or prof.get("shortName") or tk
        s = sc.get(tk) or {}
        rets = s.get("returns_pct") or {}
        v = (f.get("valuation") or {}).get("value_base")
        ter = prof.get("expenseRatioPct")
        rows.append([
            Paragraph(f"<b>{_esc(tk)}</b> {_esc(nm)}", S["cellsm"]),
            # The fund's own primary classification, as the summary's
            # "Funds by primary asset class" counts it.
            Paragraph(_esc(_humanise(f.get("effective_asset_class") or "unknown")), S["cellsm"]),
            _money(v, cur), _pct(v / total * 100 if (v and total) else None),
            *[_pct(rets.get(k), signed=True) for k, _t in ret_keys],
            "\u2014" if ter is None else f"{float(ter):.2f}%",
            _fund_size(prof.get("totalNetAssets"), prof.get("currency")),
            str(s["peer_n"]) if s.get("peer_n") else "\u2014",
            *[_rank(rs.get(tk) or {}) for _l, rs in rank_cols],
        ])
    widths = ([land_w * .2, land_w * .075, land_w * .07, land_w * .045]
              + [land_w * .05] * len(ret_keys)
              + [land_w * .045, land_w * .08, land_w * .04])
    rest = land_w - sum(widths)
    widths += [rest / max(1, len(rank_cols))] * len(rank_cols)
    r0 = n_fixed + len(ret_keys) + 3
    story.append(_table(rows, widths, header_rows=2, size=7, extra=[
        ("ALIGN", (1, 1), (1, -1), "LEFT"),
        ("SPAN", (n_fixed, 0), (n_fixed + len(ret_keys) - 1, 0)),
        ("SPAN", (r0, 0), (-1, 0)),
        ("ALIGN", (n_fixed, 0), (-1, 0), "CENTER"),
        ("LINEBELOW", (n_fixed, 0), (n_fixed + len(ret_keys) - 1, 0), 0.4,
         colors.HexColor("#d1d5db")),
        ("LINEBELOW", (r0, 0), (-1, 0), 0.4, colors.HexColor("#d1d5db")),
    ]))
    story.append(Paragraph(
        f"Value in {cur}; weight is the share of the whole portfolio. Returns are trailing "
        "price returns. TER is the yearly cost; Size the fund's assets in "
        "its own currency. Peers is the number of saved funds in the fund's "
        "peer group (same asset class and focus), itself included. Each Rank "
        "column is the fund's score under that weight model (Cost driven, Cost and returns, Returns driven), 0\u2013100 with 100 "
        "best: first against all saved funds, then against its peers.",
        S["note"]))
    check()

    # ── 3. History ───────────────────────────────────────────────────────
    say("drawing the history")
    story.append(NextPageTemplate("portrait"))
    story.append(PageBreak())
    story.append(Paragraph(f"3. Portfolio history — {PERIOD_TITLES.get(period, period)}, indexed", S["h1"]))
    h = history_series(ph, period, include_cash)
    if h:
        story.append(_history_chart(h, page_w))
        bits = [f"Portfolio {h['return']:+.1f}% over the period"
                f" ({'including' if include_cash else 'excluding'} the cash held by you)."]
        if h.get("best"):
            bits.append(f"Best fund: {_esc(h['best']['ticker'] or '')} — {_esc(h['best']['name'] or '')} "
                        f"({h['best']['return']:+.1f}%).")
        if h.get("worst"):
            bits.append(f"Worst fund: {_esc(h['worst']['ticker'] or '')} — {_esc(h['worst']['name'] or '')} "
                        f"({h['worst']['return']:+.1f}%).")
        if h.get("excluded_young"):
            bits.append(f"{h['excluded_young']} fund(s) younger than the period "
                        f"are not ranked for best/worst.")
        bits.append("The 50- and 200-day averages are of the portfolio, "
                    "computed on its full history.")
        story.append(Paragraph(" ".join(bits), S["note"]))
    else:
        story.append(Paragraph("No price history is available for this portfolio.", S["body"]))
    check()

    # ── 4. Exposure vs target ────────────────────────────────────────────
    say("laying out the targets")
    story.append(Paragraph("4. Exposure versus target", S["h1"]))
    td = view.get("target_deviations") or {}
    tfacets = td.get("facets") or {}
    if not td.get("any_targets"):
        story.append(Paragraph("No targets are set for this portfolio.", S["body"]))
    else:
        story.append(Paragraph(
            "Percentages are of the fund side — what remains after the cash "
            "held by you — exactly as the Targets tab measures them. A bucket "
            "is within target when it sits inside its allowed miss.", S["note"]))
        green, amber = colors.HexColor("#15803d"), colors.HexColor("#b45309")
        for facet in TARGET_FACETS:
            blk = tfacets.get(facet) or {}
            if not blk.get("has_targets"):
                continue
            levels = FACET_LEVELS.get(facet) or (facet,)
            for lvl in reversed(levels):              # coarse to fine
                lb = (blk.get("levels") or {}).get(lvl) or {}
                items = lb.get("items") or []
                if not items:
                    continue
                rows = [[f"{FACET_TITLES.get(facet, facet)} — {LEVEL_TITLES.get(lvl, lvl) if len(levels) > 1 else ''}".rstrip(" —"),
                         "Target", "Now", "Deviation", "Allowed miss", ""]]
                extra = []
                for n, it in enumerate(sorted(items, key=lambda i: -(i.get("target") or 0)), 1):
                    ok = it.get("within")
                    unm = it.get("unmeasurable")
                    rows.append([
                        Paragraph(_esc(bucket_label(facet, it["key"], labels)), S["cell"]),
                        _pct((it.get("target") or 0) * 100),
                        "—" if unm else _pct((it.get("actual") or 0) * 100),
                        # Points, not percent: the gap between two shares.
                        "—" if unm else f"{(it.get('deviation') or 0) * 100:+.1f} pt",
                        f"±{(it.get('allowance') or 0) * 100:.1f} pt",
                        "n/m" if unm else ("✓" if ok else "✗"),
                    ])
                    if not unm:
                        extra.append(("TEXTCOLOR", (5, n), (5, n), green if ok else amber))
                        extra.append(("FONT", (5, n), (5, n), bold, 8))
                if lb.get("untargeted_pct"):
                    rows.append([Paragraph("<i>untargeted (the optimiser's free choice)</i>", S["cell"]),
                                 "", _pct(lb["untargeted_pct"] * 100), "", "", ""])
                story.append(KeepTogether([_table(
                    rows, [page_w * .4, page_w * .12, page_w * .12, page_w * .13,
                           page_w * .12, page_w * .11], extra=extra), Spacer(1, 6)]))
        untargeted = [FACET_TITLES[f] for f in TARGET_FACETS
                      if not (tfacets.get(f) or {}).get("has_targets")]
        if untargeted:
            story.append(Paragraph("No targets set for: " + ", ".join(untargeted) + ".", S["note"]))
    check()

    # ── 5. X-ray ─────────────────────────────────────────────────────────
    say("drawing the X-ray")
    story.append(PageBreak())
    story.append(Paragraph("5. Portfolio X-ray", S["h1"]))
    story.append(Paragraph(
        f"Look-through exposure at each facet's middle level, as a share of "
        f"the whole portfolio{'' if include_cash else ' excluding the cash held by you'}. "
        f"Grey bars are what the data cannot place (unknown) or where the "
        f"question does not apply.", S["note"]))
    bd = view.get("fundlevel_breakdowns" if include_cash
                  else "fundlevel_breakdowns_ex_cash") or {}
    cells = []
    for facet in list(BREAKDOWN_FACETS) + [f for f in META_FACETS if f in TARGET_FACETS]:
        blk = bd.get(facet) or {}
        lvl = middle_level(facet)
        items = (blk.get("items") or {}).get(lvl) or []
        cov = (blk.get("coverage") or {}).get(lvl)
        head = Paragraph(
            f"<b>{FACET_TITLES.get(facet, facet)}</b> — {LEVEL_TITLES.get(lvl, lvl)}"
            + (f" · {cov * 100:.0f}% identified" if cov is not None else ""), S["cell"])
        body = (_xray_chart(facet, items, labels, page_w / 2 - 8) if items
                else Paragraph("No data.", S["note"]))
        cells.append([head, body])
        check()
    grid = []
    for i in range(0, len(cells), 2):
        pair = cells[i:i + 2] + ([["", ""]] if len(cells[i:i + 2]) == 1 else [])
        grid.append([pair[0][0], pair[1][0]])
        grid.append([pair[0][1], pair[1][1]])
    from reportlab.platypus import TableStyle
    g = Table(grid, colWidths=[page_w / 2, page_w / 2])
    g.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(g)

    # ── 6. Addendum index ────────────────────────────────────────────────
    factsheets = factsheets or []
    if factsheets:
        story.append(PageBreak())
        story.append(Paragraph("6. Addendum — fund factsheets", S["h1"]))
        rows = [["Fund", "Factsheet"]]
        for fs in factsheets:
            rows.append([Paragraph(f"<b>{_esc(fs['ticker'])}</b> {_esc(fs.get('name') or '')}", S["cell"]),
                         "appended" if fs.get("path") else "none stored"])
        story.append(_table(rows, [page_w * .8, page_w * .2]))

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(reg, 7)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawString(15 * mm, 10 * mm, f"PorxPy {app_version} · {name}")
        canvas.drawRightString(canvas._pagesize[0] - 15 * mm, 10 * mm, f"page {doc.page}")
        canvas.restoreState()

    say("writing the PDF")
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, title=f"Portfolio overview — {name}",
                          author="PorxPy")

    def _template(tid: str, size) -> PageTemplate:
        # The same margins either way round; only the funds table turns.
        frame = Frame(15 * mm, 16 * mm, size[0] - 30 * mm, size[1] - 30 * mm,
                      id=tid, leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        return PageTemplate(id=tid, frames=[frame], pagesize=size, onPage=_footer)
    doc.addPageTemplates([_template("portrait", A4),
                          _template("landscape", (A4[1], A4[0]))])
    doc.build(story)
    main = buf.getvalue()

    attach = [fs for fs in factsheets if fs.get("path")]
    if not attach:
        return main
    return _append_factsheets(main, attach, say, check)


def _image_page(path: Path) -> bytes:
    """One A4 page holding an image factsheet, scaled to fit."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    img = ImageReader(str(path))
    iw, ih = img.getSize()
    m = 28
    scale = min((A4[0] - 2 * m) / iw, (A4[1] - 2 * m) / ih)
    w, h = iw * scale, ih * scale
    c.drawImage(img, (A4[0] - w) / 2, (A4[1] - h) / 2, w, h)
    c.showPage()
    c.save()
    return buf.getvalue()


def _append_factsheets(main: bytes, attach: list[dict], say, check) -> bytes:
    """The report followed by each factsheet, with a bookmark per fund.

    A factsheet that cannot be read is skipped rather than failing the
    whole report — the addendum index already says which funds have one,
    and a damaged document is no reason to withhold the rest.
    """
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(main)))
    w.add_outline_item("Portfolio overview", 0)
    for n, fs in enumerate(attach, 1):
        check()
        say(f"appending factsheet {n} of {len(attach)} ({fs['ticker']})")
        first = len(w.pages)
        try:
            ext = (fs.get("ext") or "").lower()
            data = (Path(fs["path"]).read_bytes() if ext == ".pdf"
                    else _image_page(Path(fs["path"])))
            w.append(PdfReader(io.BytesIO(data)))
        except Exception as exc:                      # pragma: no cover
            print(f"[Report] factsheet for {fs['ticker']} skipped: {exc}")
            continue
        if len(w.pages) > first:
            w.add_outline_item(f"{fs['ticker']} — {fs.get('name') or ''}", first)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()
