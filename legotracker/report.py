"""Builds the self-contained dashboard HTML from whatever is in the database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .analytics import (DealVerdict, PriceStats, compute_stats, score_deal,
                        value_per_piece)
from .db import Database
from .sources import PRICE_SOURCES, RETAILER_LABELS

# Fixed categorical slots, assigned by retailer identity and never cycled.
# Validated in both modes on the adjacent pairlist; the light-mode contrast
# warning on aqua/yellow/magenta is relieved by the always-visible retailer
# names beside each swatch and by the screener table itself.
RETAILER_COLOUR = {
    "amazon_in":  {"light": "#2a78d6", "dark": "#3987e5"},   # slot 1 blue
    "flipkart":   {"light": "#eb6834", "dark": "#d95926"},   # slot 2 orange
    "firstcry":   {"light": "#1baf7a", "dark": "#199e70"},   # slot 3 aqua
    "hamleys":    {"light": "#eda100", "dark": "#c98500"},   # slot 4 yellow
    "jaimantoys": {"light": "#e87ba4", "dark": "#d55181"},   # slot 5 magenta
    "toycra":     {"light": "#4a3aa7", "dark": "#9085e9"},   # slot 7 violet
}


def _fmt_ts(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def build_payload(db: Database) -> dict:
    """Everything the dashboard needs, as plain JSON-safe structures."""
    sets_out: list[dict] = []
    targets = db.targets()

    for row in db.watched_sets():
        set_num = row["set_num"]
        offers: list[dict] = []
        series: list[dict] = []

        for listing in db.listings_for(set_num):
            retailer = listing["retailer"]
            history = [dict(h) for h in db.history(listing["id"])]
            stats = compute_stats(history)

            if retailer in PRICE_SOURCES and stats.current is not None:
                latest = db.latest_price(listing["id"])
                verdict = score_deal(
                    stats,
                    mrp=latest["mrp_inr"] if latest else None,
                    target=targets.get(set_num),
                )
                offers.append({
                    "retailer": retailer,
                    "label": RETAILER_LABELS.get(retailer, retailer),
                    "url": listing["url"],
                    "title": listing["title"],
                    "price": stats.current,
                    "mrp": latest["mrp_inr"] if latest else None,
                    "in_stock": bool(latest["in_stock"]) if latest and latest["in_stock"] is not None else None,
                    "low": stats.low,
                    "high": stats.high,
                    "typical": stats.typical,
                    "n": stats.n,
                    "score": verdict.score,
                    "verdict": verdict.label,
                    "reasons": verdict.reasons,
                    "confidence": verdict.confidence,
                    "vs_typical": verdict.vs_typical_pct,
                    "vs_mrp": verdict.vs_mrp_pct,
                    "all_time_low": verdict.is_all_time_low,
                })

            points = [
                {"t": _fmt_ts(h["observed_at"]), "p": h["price_inr"]}
                for h in reversed(history)
                if h["price_inr"] is not None
            ]
            if points and retailer in PRICE_SOURCES:
                series.append({
                    "retailer": retailer,
                    "label": RETAILER_LABELS.get(retailer, retailer),
                    "points": points,
                })

        official = any(l["retailer"] == "lego_in" for l in db.listings_for(set_num))

        best = min(offers, key=lambda o: o["price"]) if offers else None
        sets_out.append({
            "set_num": set_num,
            "name": row["name"] or (best["title"] if best else f"Set {set_num}"),
            "theme": row["theme"],
            "pieces": row["pieces"],
            "year": row["year"],
            "official_in": official,
            "target": targets.get(set_num),
            "best": best,
            "offers": sorted(offers, key=lambda o: o["price"]),
            "series": series,
            "per_piece": value_per_piece(best["price"] if best else None,
                                         row["pieces"]),
        })

    # Sets with a live price first, best deal score first within that.
    sets_out.sort(key=lambda s: (s["best"] is None,
                                 -(s["best"]["score"] if s["best"] else 0)))

    runs = [dict(r) for r in db.recent_runs(12)]
    alerts = [dict(a) for a in db.open_alerts(40)]
    for a in alerts:
        a["created_at"] = _fmt_ts(a["created_at"])

    priced = [s for s in sets_out if s["best"]]
    return {
        "generated_at": datetime.now(timezone.utc).astimezone().strftime(
            "%d %b %Y, %H:%M"),
        "sets": sets_out,
        "alerts": alerts,
        "runs": runs,
        "colours": RETAILER_COLOUR,
        "summary": {
            "sets_tracked": len(sets_out),
            "sets_priced": len(priced),
            "listings": sum(len(s["offers"]) for s in sets_out),
            "strong_buys": len([s for s in priced
                                if s["best"]["score"] >= 80]),
            "all_time_lows": len([s for s in priced
                                  if s["best"]["all_time_low"]]),
            "last_run": _fmt_ts(runs[0]["started_at"]) if runs else None,
        },
    }


def render(db: Database, out_path: str | Path) -> Path:
    payload = build_payload(db)
    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload))
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LEGO Price Tracker — India</title>
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,0.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
header{display:flex;justify-content:space-between;align-items:flex-end;
  gap:16px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:21px;margin:0 0 4px;letter-spacing:-0.01em}
.sub{color:var(--ink-2);font-size:13px}
button{font:inherit;color:inherit;background:var(--surface);cursor:pointer;
  border:1px solid var(--ring);border-radius:8px;padding:6px 11px}
button:hover{border-color:var(--axis)}
button[aria-pressed="true"]{background:var(--ink);color:var(--surface);border-color:var(--ink)}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:12px;margin-bottom:22px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:14px 16px}
.tile .k{color:var(--muted);font-size:12px;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:6px}
.tile .v{font-size:26px;font-weight:600;letter-spacing:-0.02em}
.tile .n{color:var(--ink-2);font-size:12px;margin-top:2px}

section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:18px;margin-bottom:20px}
h2{font-size:15px;margin:0 0 4px}
.hint{color:var(--ink-2);font-size:12.5px;margin:0 0 14px}

.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
input[type=search]{font:inherit;padding:6px 10px;border-radius:8px;
  border:1px solid var(--ring);background:var(--plane);color:var(--ink);min-width:200px}

.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--ink-2);padding:8px 10px;
  border-bottom:1px solid var(--axis);white-space:nowrap;font-size:12px;
  text-transform:uppercase;letter-spacing:.04em;cursor:pointer;user-select:none}
th:hover{color:var(--ink)}
td{padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.row{cursor:pointer}
tr.row:hover td{background:color-mix(in srgb,var(--ink) 4%,transparent)}
.setname{font-weight:500}
.setmeta{color:var(--muted);font-size:12px}

.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;
  border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;
  border:1px solid transparent}
.badge .dot{width:7px;height:7px;border-radius:50%;flex:none}
.b-strong{color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent)}
.b-good{color:var(--good);border-color:color-mix(in srgb,var(--good) 25%,transparent)}
.b-fair{color:var(--ink-2);border-color:var(--ring)}
.b-wait{color:var(--serious);border-color:color-mix(in srgb,var(--serious) 45%,transparent)}
.b-over{color:var(--critical);border-color:color-mix(in srgb,var(--critical) 40%,transparent)}
.b-watch{color:var(--muted);border-color:var(--ring)}

.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)}
.swatch{width:9px;height:9px;border-radius:2px;flex:none}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:2px 0 10px}

.detail{background:var(--plane)}
.detail td{padding:0}
.panel{padding:16px 12px 20px}
.panelgrid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:20px}
@media(max-width:820px){.panelgrid{grid-template-columns:1fr}}
.offers{list-style:none;margin:0;padding:0}
.offers li{display:flex;justify-content:space-between;gap:10px;padding:8px 0;
  border-bottom:1px solid var(--grid);align-items:baseline}
.offers a{color:inherit}
.reasons{margin:8px 0 0;padding-left:16px;color:var(--ink-2);font-size:12.5px}
.reasons li{margin:2px 0}

svg{display:block;overflow:visible}
.gridline{stroke:var(--grid);stroke-width:1}
.baseline{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px}
.dlabel{font-size:11.5px;font-weight:600;paint-order:stroke;
  stroke:var(--surface);stroke-width:3px;stroke-linejoin:round}
.crosshair{stroke:var(--axis);stroke-width:1;stroke-dasharray:3 3}
.tip{position:fixed;pointer-events:none;z-index:50;background:var(--surface);
  border:1px solid var(--ring);border-radius:9px;padding:8px 10px;font-size:12.5px;
  box-shadow:0 6px 22px rgba(0,0,0,.13);opacity:0;transition:opacity .1s}
.tip b{font-variant-numeric:tabular-nums}
.tiprow{display:flex;align-items:center;gap:7px;margin-top:3px}
.empty{color:var(--ink-2);padding:26px 4px;text-align:center}
.alert{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--grid);
  align-items:baseline;font-size:13px}
.alert .when{color:var(--muted);font-size:12px;white-space:nowrap}
.note{font-size:12.5px;color:var(--ink-2);margin-top:10px}
code{background:var(--plane);padding:1px 5px;border-radius:5px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>LEGO Price Tracker — India</h1>
    <div class="sub" id="sub"></div>
  </div>
  <button id="theme" aria-pressed="false">Dark mode</button>
</header>

<div class="tiles" id="tiles"></div>

<section id="alerts-sec" hidden>
  <h2>Alerts</h2>
  <p class="hint">Raised on the last collection run.</p>
  <div id="alerts"></div>
</section>

<section>
  <h2>Screener</h2>
  <p class="hint">Cheapest live offer per set. <strong>Score</strong> compares today's
  price against that set's own trading history, not against MRP — a big discount off
  an inflated MRP is not a deal. Click a row for price history.</p>
  <div class="filters">
    <input type="search" id="q" placeholder="Filter by set number, name or theme">
    <button data-f="all" aria-pressed="true">All</button>
    <button data-f="deals" aria-pressed="false">Good price or better</button>
    <button data-f="low" aria-pressed="false">At all-time low</button>
    <button data-f="target" aria-pressed="false">Below my target</button>
  </div>
  <div class="legend" id="legend"></div>
  <div class="tablewrap">
    <table id="tbl">
      <thead><tr>
        <th data-s="set_num">Set</th>
        <th data-s="name">Name</th>
        <th data-s="price" class="num">Best price</th>
        <th data-s="retailer">Where</th>
        <th data-s="vs_typical" class="num">vs typical</th>
        <th data-s="vs_mrp" class="num">vs MRP</th>
        <th data-s="per_piece" class="num">₹/piece</th>
        <th data-s="score">Verdict</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <p class="note" id="count"></p>
</section>

<section>
  <h2>Collection runs</h2>
  <p class="hint">If a run says <em>partial</em> or <em>failed</em>, a retailer
  changed its layout or blocked the scraper — run <code>python -m legotracker diagnose</code>.</p>
  <div class="tablewrap"><table>
    <thead><tr><th>Started</th><th>Status</th><th class="num">Sets</th>
      <th class="num">Prices saved</th><th>Notes</th></tr></thead>
    <tbody id="runs"></tbody>
  </table></div>
</section>
</div>
<div class="tip" id="tip"></div>

<script>
const DATA = __PAYLOAD__;
const $ = s => document.querySelector(s);
const inr = n => n == null ? "—" : "₹" + Math.round(n).toLocaleString("en-IN");
const pct = n => n == null ? "—" : (n > 0 ? "−" : "+") + Math.abs(n).toFixed(1) + "%";

/* ---------- theme ---------- */
const themeBtn = $("#theme");
const prefersDark = matchMedia("(prefers-color-scheme: dark)").matches;
if (prefersDark) {
  document.documentElement.setAttribute("data-theme", "dark");
  themeBtn.textContent = "Light mode";
  themeBtn.setAttribute("aria-pressed", "true");
}
themeBtn.onclick = () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  themeBtn.textContent = dark ? "Dark mode" : "Light mode";
  themeBtn.setAttribute("aria-pressed", String(!dark));
  document.querySelectorAll("tr.detail:not([hidden])").forEach(tr => {
    const host = tr.querySelector(".chart");
    if (host) drawChart(host, DATA.sets.find(s => s.set_num === tr.dataset.set));
  });
};

/* ---------- header + tiles ---------- */
const S = DATA.summary;
$("#sub").textContent =
  `Generated ${DATA.generated_at} · ${S.sets_priced} of ${S.sets_tracked} tracked sets have a live price`
  + (S.last_run ? ` · last collection ${S.last_run}` : "");

$("#tiles").innerHTML = [
  ["Sets tracked", S.sets_tracked, `${S.listings} listings across retailers`],
  ["Live prices", S.sets_priced, "sets with a current offer"],
  ["Strong buys", S.strong_buys, "score 80+ vs own history"],
  ["All-time lows", S.all_time_lows, "cheapest we have recorded"],
].map(([k, v, n]) =>
  `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`
).join("");

/* ---------- legend (identity is never colour-alone) ---------- */
const RET = ["amazon_in", "flipkart", "firstcry", "hamleys", "jaimantoys", "toycra"];
const colourOf = r => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches
    ? document.documentElement.getAttribute("data-theme") !== "light"
    : document.documentElement.getAttribute("data-theme") === "dark";
  return (DATA.colours[r] || {})[dark ? "dark" : "light"] || "#888";
};
const LABEL = {amazon_in: "Amazon.in", flipkart: "Flipkart", firstcry: "FirstCry",
               hamleys: "Hamleys", jaimantoys: "Jaiman Toys", toycra: "Toycra"};
$("#legend").innerHTML = RET.map(r =>
  `<span class="chip"><span class="swatch" style="background:${colourOf(r)}"></span>${LABEL[r]}</span>`
).join("") + `<span class="chip" style="color:var(--muted)">LEGO.com IN is tracked for
  official availability only — it does not publish prices in page HTML.</span>`;

/* ---------- alerts ---------- */
if (DATA.alerts.length) {
  $("#alerts-sec").hidden = false;
  $("#alerts").innerHTML = DATA.alerts.map(a =>
    `<div class="alert"><span class="when">${a.created_at}</span>
     <span>${esc(a.message)}</span></div>`).join("");
}

/* ---------- screener ---------- */
let filter = "all", query = "", sortKey = "score", sortDir = -1;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
}

function badge(o) {
  if (!o) return `<span class="badge b-watch"><span class="dot" style="background:var(--muted)"></span>No price</span>`;
  const map = {
    "Strong buy": ["b-strong", "var(--good)", "▲"],
    "Good price": ["b-good", "var(--good)", "▲"],
    "Fair": ["b-fair", "var(--muted)", "■"],
    "Wait": ["b-wait", "var(--serious)", "▼"],
    "Overpriced": ["b-over", "var(--critical)", "▼"],
    "Watching": ["b-watch", "var(--muted)", "◷"],
  };
  const [cls, col, icon] = map[o.verdict] || map["Fair"];
  return `<span class="badge ${cls}" title="score ${o.score}/100, ${o.confidence} confidence">
    <span class="dot" style="background:${col}"></span>${icon} ${o.verdict}</span>`;
}

function rowsFor() {
  let rows = DATA.sets.slice();
  if (filter === "deals") rows = rows.filter(s => s.best && s.best.score >= 65);
  if (filter === "low") rows = rows.filter(s => s.best && s.best.all_time_low);
  if (filter === "target") rows = rows.filter(s => s.target && s.best && s.best.price <= s.target);
  if (query) {
    const q = query.toLowerCase();
    rows = rows.filter(s => (s.set_num + " " + (s.name || "") + " " + (s.theme || ""))
      .toLowerCase().includes(q));
  }
  const get = s => ({
    set_num: +s.set_num || 0,
    name: (s.name || "").toLowerCase(),
    price: s.best ? s.best.price : Infinity,
    retailer: s.best ? s.best.label : "",
    vs_typical: s.best && s.best.vs_typical != null ? s.best.vs_typical : -999,
    vs_mrp: s.best && s.best.vs_mrp != null ? s.best.vs_mrp : -999,
    per_piece: s.per_piece == null ? Infinity : s.per_piece,
    score: s.best ? s.best.score : -1,
  }[sortKey]);
  return rows.sort((a, b) => {
    const x = get(a), y = get(b);
    if (x === y) return 0;
    return (x > y ? 1 : -1) * sortDir;
  });
}

function draw() {
  const rows = rowsFor();
  $("#tbody").innerHTML = rows.map(s => {
    const o = s.best;
    return `<tr class="row" data-set="${s.set_num}">
      <td class="num">${s.set_num}</td>
      <td><div class="setname">${esc(s.name).slice(0, 62)}</div>
          <div class="setmeta">${[s.theme, s.pieces ? s.pieces + " pcs" : null,
            s.official_in ? "on LEGO.com IN" : null].filter(Boolean).join(" · ") || "&nbsp;"}</div></td>
      <td class="num"><strong>${inr(o && o.price)}</strong></td>
      <td>${o ? `<span class="chip"><span class="swatch" style="background:${colourOf(o.retailer)}"></span>${o.label}</span>` : "—"}</td>
      <td class="num">${o ? pct(o.vs_typical) : "—"}</td>
      <td class="num">${o ? pct(o.vs_mrp) : "—"}</td>
      <td class="num">${s.per_piece == null ? "—" : "₹" + s.per_piece.toFixed(2)}</td>
      <td>${badge(o)}</td>
    </tr>
    <tr class="detail" data-set="${s.set_num}" hidden><td colspan="8"></td></tr>`;
  }).join("") || `<tr><td colspan="8" class="empty">Nothing matches that filter.</td></tr>`;

  $("#count").textContent =
    `${rows.length} of ${DATA.sets.length} sets shown. "vs typical" is negative when today is cheaper than the set's own median.`;
}

$("#q").oninput = e => { query = e.target.value; draw(); };
document.querySelectorAll("[data-f]").forEach(b => b.onclick = () => {
  document.querySelectorAll("[data-f]").forEach(x => x.setAttribute("aria-pressed", "false"));
  b.setAttribute("aria-pressed", "true");
  filter = b.dataset.f; draw();
});
document.querySelectorAll("th[data-s]").forEach(th => th.onclick = () => {
  const k = th.dataset.s;
  sortDir = (sortKey === k) ? -sortDir : (k === "price" || k === "per_piece" ? 1 : -1);
  sortKey = k; draw();
});

/* ---------- row expansion ---------- */
$("#tbody").addEventListener("click", e => {
  const tr = e.target.closest("tr.row");
  if (!tr) return;
  const det = tr.nextElementSibling;
  if (!det || !det.classList.contains("detail")) return;
  const s = DATA.sets.find(x => x.set_num === tr.dataset.set);
  if (det.hidden) {
    det.hidden = false;
    det.firstElementChild.innerHTML = panelHTML(s);
    drawChart(det.querySelector(".chart"), s);
  } else {
    det.hidden = true;
    det.firstElementChild.innerHTML = "";
  }
});

function panelHTML(s) {
  const offers = s.offers.map(o => `<li>
      <span><span class="swatch" style="background:${colourOf(o.retailer)};display:inline-block;margin-right:6px"></span>
        <a href="${esc(o.url)}" target="_blank" rel="noopener">${o.label}</a>
        ${o.in_stock === false ? '<span style="color:var(--critical)"> · out of stock</span>' : ""}</span>
      <span class="num"><strong>${inr(o.price)}</strong>
        <span style="color:var(--muted)"> low ${inr(o.low)} · ${o.n} obs</span></span>
    </li>`).join("") || `<li style="color:var(--ink-2)">No priced offers recorded yet.</li>`;

  const best = s.best;
  const reasons = best && best.reasons.length
    ? `<ul class="reasons">${best.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`
    : "";

  return `<div class="panel"><div class="panelgrid">
    <div>
      <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">
        Price history</div>
      <div class="chart"></div>
    </div>
    <div>
      <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">
        Offers</div>
      <ul class="offers">${offers}</ul>
      ${best ? `<div style="margin-top:12px">${badge(best)}
        <span style="color:var(--ink-2);font-size:12.5px"> · ${best.confidence} confidence
        (${best.n} observation${best.n === 1 ? "" : "s"})</span></div>${reasons}` : ""}
    </div>
  </div></div>`;
}

/* ---------- line chart with crosshair ---------- */
const tip = $("#tip");

function drawChart(host, s) {
  if (!host) return;
  const series = (s.series || []).filter(x => x.points.length);
  if (!series.length) {
    host.innerHTML = `<div class="empty">No price history yet — run a few daily collections.</div>`;
    return;
  }

  const W = Math.max(360, host.clientWidth || 520), H = 240;
  const M = {t: 14, r: 66, b: 26, l: 52};
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  const dates = [...new Set(series.flatMap(x => x.points.map(p => p.t)))].sort();
  const vals = series.flatMap(x => x.points.map(p => p.p));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { hi = lo * 1.05 + 1; lo = lo * 0.95; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;

  const X = t => dates.length === 1 ? iw / 2 : (dates.indexOf(t) / (dates.length - 1)) * iw;
  const Y = v => ih - ((v - lo) / (hi - lo)) * ih;

  const ticks = 4, gl = [], yl = [];
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * i / ticks, y = Y(v);
    gl.push(`<line class="gridline" x1="0" y1="${y}" x2="${iw}" y2="${y}"/>`);
    yl.push(`<text class="tick" x="-9" y="${y + 4}" text-anchor="end">${inr(v)}</text>`);
  }

  // Direct labels are the relief for the aqua series' sub-3:1 contrast, so they
  // must stay legible: nudge them apart when two endpoints nearly coincide.
  const ends = series.map(x => {
    const pts = x.points.slice().sort((a, b) => a.t < b.t ? -1 : 1);
    const last = pts[pts.length - 1];
    return {x, pts, last, y: Y(last.p), ly: Y(last.p)};
  }).sort((a, b) => a.y - b.y);
  const MINGAP = 14;
  for (let i = 1; i < ends.length; i++) {
    if (ends[i].ly - ends[i - 1].ly < MINGAP) ends[i].ly = ends[i - 1].ly + MINGAP;
  }
  const overflow = ends.length ? ends[ends.length - 1].ly - ih : 0;
  if (overflow > 0) ends.forEach(e => e.ly -= overflow);

  const paths = ends.map(({x, pts, last, y, ly}) => {
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.p).toFixed(1)}`).join("");
    const c = colourOf(x.retailer);
    const leader = Math.abs(ly - y) > 1
      ? `<line x1="${X(last.t) + 5}" y1="${y}" x2="${X(last.t) + 8}" y2="${ly}"
           stroke="${c}" stroke-width="1" opacity="0.55"/>` : "";
    return `<path d="${d}" fill="none" stroke="${c}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>
            <circle cx="${X(last.t)}" cy="${y}" r="4"
              fill="${c}" stroke="var(--surface)" stroke-width="2"/>${leader}
            <text class="dlabel" x="${X(last.t) + 10}" y="${ly + 4}"
              fill="${c}">${inr(last.p)}</text>`;
  }).join("");

  const xl = [dates[0], dates[dates.length - 1]].filter((v, i, a) => a.indexOf(v) === i)
    .map((t, i, a) => `<text class="tick" x="${X(t)}" y="${ih + 18}"
       text-anchor="${a.length === 1 ? "middle" : (i ? "end" : "start")}">${t}</text>`).join("");

  host.innerHTML = `<svg width="100%" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Price history by retailer">
    <g transform="translate(${M.l},${M.t})">
      ${gl.join("")}${yl.join("")}
      <line class="baseline" x1="0" y1="${ih}" x2="${iw}" y2="${ih}"/>
      ${paths}${xl}
      <line class="crosshair" id="ch" y1="0" y2="${ih}" x1="-99" x2="-99"/>
      <rect x="0" y="0" width="${iw}" height="${ih}" fill="transparent" id="hit"/>
    </g></svg>`;

  const svg = host.querySelector("svg");
  const hit = host.querySelector("#hit"), ch = host.querySelector("#ch");
  hit.addEventListener("mousemove", ev => {
    const box = svg.getBoundingClientRect();
    const scale = box.width / W;
    const px = (ev.clientX - box.left) / scale - M.l;
    const idx = dates.length === 1 ? 0
      : Math.max(0, Math.min(dates.length - 1, Math.round(px / iw * (dates.length - 1))));
    const t = dates[idx];
    ch.setAttribute("x1", X(t)); ch.setAttribute("x2", X(t));
    const rows = series.map(x => {
      const p = x.points.find(p => p.t === t);
      return p ? `<div class="tiprow"><span class="swatch"
        style="background:${colourOf(x.retailer)}"></span>${x.label}
        <b style="margin-left:auto">${inr(p.p)}</b></div>` : "";
    }).join("");
    tip.innerHTML = `<div style="color:var(--muted)">${t}</div>${rows}`;
    tip.style.opacity = 1;
    tip.style.left = Math.min(window.innerWidth - 190, ev.clientX + 14) + "px";
    tip.style.top = (ev.clientY - 12) + "px";
  });
  hit.addEventListener("mouseleave", () => {
    tip.style.opacity = 0; ch.setAttribute("x1", -99); ch.setAttribute("x2", -99);
  });
}

/* ---------- runs ---------- */
$("#runs").innerHTML = DATA.runs.map(r => `<tr>
  <td>${(r.started_at || "").replace("T", " ").slice(0, 16)}</td>
  <td>${r.status || ""}</td><td class="num">${r.sets_seen ?? ""}</td>
  <td class="num">${r.prices_saved ?? ""}</td>
  <td style="color:var(--ink-2)">${esc((r.notes || "").slice(0, 90))}</td></tr>`).join("")
  || `<tr><td colspan="5" class="empty">No runs recorded yet.</td></tr>`;

draw();
</script>
</body></html>
"""
