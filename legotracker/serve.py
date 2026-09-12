"""Local search web app.

Deliberately built on `http.server` from the standard library rather than Flask
or FastAPI — the whole tool stays at three dependencies, and this does not need
to be a real web server: it binds to localhost, serves one page and one JSON
endpoint, and only you talk to it.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .db import Database
from .http_client import PoliteSession
from .search import SearchOutcome, search, _persist
from .sources import RETAILER_LABELS, SEARCH_SOURCES

log = logging.getLogger(__name__)

# Fixed categorical slots, assigned by retailer identity and never cycled.
RETAILER_COLOUR = {
    "amazon_in":    {"light": "#2a78d6", "dark": "#3987e5"},  # slot 1 blue
    "flipkart":     {"light": "#eb6834", "dark": "#d95926"},  # slot 2 orange
    "firstcry":     {"light": "#1baf7a", "dark": "#199e70"},  # slot 3 aqua
    "hamleys":      {"light": "#eda100", "dark": "#c98500"},  # slot 4 yellow
    "jaimantoys":   {"light": "#e87ba4", "dark": "#d55181"},  # slot 5 magenta
    "mybrickhouse": {"light": "#008300", "dark": "#008300"},  # slot 6 green
    "toycra":       {"light": "#4a3aa7", "dark": "#9085e9"},  # slot 7 violet
}


def outcome_to_json(o: SearchOutcome) -> dict:
    return {
        "query": o.query,
        "kind": o.kind,
        "elapsed_ms": o.elapsed_ms,
        "rejected": o.rejected,
        "statuses": [
            {"retailer": s.retailer, "label": s.label, "ok": s.ok,
             "returned": s.returned, "matched": s.matched,
             "error": s.error, "reason": s.reason, "ms": s.ms,
             "cached_age_s": s.cached_age_s}
            for s in o.statuses
        ],
        "sets": [
            {
                "set_num": s.set_num,
                "name": s.name,
                "spread": s.spread,
                "best": s.best.retailer if s.best else None,
                "offers": [
                    {"retailer": of.retailer,
                     "label": RETAILER_LABELS.get(of.retailer, of.retailer),
                     "title": of.title, "url": of.url,
                     "price": of.price_inr, "mrp": of.mrp_inr,
                     "in_stock": of.in_stock, "rating": of.rating}
                    for of in s.offers
                ],
            }
            for s in o.sets
        ],
    }


class Handler(BaseHTTPRequestHandler):
    session: PoliteSession
    db: Optional[Database] = None
    db_lock = threading.Lock()

    def log_message(self, fmt, *args):        # quieter than the default
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                 # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/catalog":
            try:
                from . import catalog as catalog_mod
                if self.db is None:
                    self._send(200, json.dumps(
                        {"items": [], "themes": [],
                         "error": "catalogue needs the database (do not use --no-log)"}
                    ).encode(), "application/json")
                    return
                with self.db_lock:
                    items = catalog_mod.load(self.db)
                    if not items and (parse_qs(parsed.query).get("refresh")
                                      or [""])[0] != "0":
                        catalog_mod.refresh(self.db, session=self.session)
                        items = catalog_mod.load(self.db)
                    age = self.db.catalog_age()
                payload = {"items": items,
                           "themes": catalog_mod.themes(items),
                           "refreshed_at": age}
                self._send(200, json.dumps(payload).encode(), "application/json")
            except Exception as exc:  # noqa: BLE001
                log.exception("catalogue failed")
                self._send(500, json.dumps({"error": str(exc)[:300]}).encode(),
                           "application/json")
            return

        if parsed.path == "/api/catalog/refresh":
            try:
                if self.db is None:
                    raise RuntimeError("catalogue needs the database")
                with self.db_lock:
                    n = __import__("legotracker.catalog", fromlist=["refresh"]) \
                        .refresh(self.db, session=self.session)
                self._send(200, json.dumps({"count": n}).encode(),
                           "application/json")
            except Exception as exc:  # noqa: BLE001
                log.exception("catalogue refresh failed")
                self._send(500, json.dumps({"error": str(exc)[:300]}).encode(),
                           "application/json")
            return

        if parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            q = (params.get("q") or [""])[0].strip()
            if not q:
                self._send(400, b'{"error":"empty query"}', "application/json")
                return
            srcs = (params.get("sources") or [""])[0]
            sources = [s for s in srcs.split(",") if s] or None
            refine = (params.get("refine") or ["1"])[0] != "0"
            try:
                # The search itself must NOT hold the database lock: it is
                # mostly network wait, and holding the lock across it makes a
                # second search queue behind the first for its entire duration.
                # Only the write is serialised.
                outcome = search(q, session=self.session, sources=sources,
                                 refine=refine, db=None)
                if self.db is not None:
                    with self.db_lock:
                        _persist(self.db, outcome)
                payload = json.dumps(outcome_to_json(outcome)).encode("utf-8")
                self._send(200, payload, "application/json")
            except Exception as exc:  # noqa: BLE001
                log.exception("search failed")
                self._send(500, json.dumps({"error": str(exc)[:300]}).encode(),
                           "application/json")
            return

        self._send(404, b"not found", "text/plain")


def serve(port: int = 8765, db_path: Optional[str] = None,
          open_browser: bool = True, delay: float = 1.0) -> None:
    # Interactive settings: short request timeout, one retry, tiny backoff.
    # A search that takes a minute is a broken search, even if it eventually
    # succeeds.
    Handler.session = PoliteSession(min_interval=delay, jitter=delay * 0.4,
                                    timeout=12, max_retries=2, backoff_cap=3.0)
    Handler.db = Database(db_path) if db_path else None

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  LEGO search running at {url}")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        if Handler.db:
            Handler.db.close()


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LEGO Search — India</title>
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,0.10);
  --good:#0ca30c; --critical:#d03b3b; --warning:#fab219;
  --amazon_in:#2a78d6; --flipkart:#eb6834; --firstcry:#1baf7a;
  --hamleys:#eda100; --jaimantoys:#e87ba4; --mybrickhouse:#008300;
  --toycra:#4a3aa7;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
  --amazon_in:#3987e5; --flipkart:#d95926; --firstcry:#199e70;
  --hamleys:#c98500; --jaimantoys:#d55181; --mybrickhouse:#008300;
  --toycra:#9085e9;
}}
:root[data-theme="dark"]{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,0.10);
  --amazon_in:#3987e5; --flipkart:#d95926; --firstcry:#199e70;
  --hamleys:#c98500; --jaimantoys:#d55181; --mybrickhouse:#008300;
  --toycra:#9085e9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:34px 20px 80px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:14px;
  flex-wrap:wrap;margin-bottom:20px}
h1{font-size:20px;margin:0;letter-spacing:-0.01em}
.sub{color:var(--ink-2);font-size:13px;margin-top:3px}
button{font:inherit;color:inherit;background:var(--surface);cursor:pointer;
  border:1px solid var(--ring);border-radius:8px;padding:6px 11px}
button:hover{border-color:var(--axis)}

form{display:flex;gap:9px;margin-bottom:8px}
#q{flex:1;font:16px/1.4 inherit;padding:12px 15px;border-radius:11px;
  border:1px solid var(--ring);background:var(--surface);color:var(--ink)}
#q:focus{outline:2px solid var(--amazon_in);outline-offset:-1px;border-color:transparent}
#go{padding:0 22px;font-weight:600;border-radius:11px}
.examples{color:var(--muted);font-size:12.5px;margin-bottom:22px}
.examples b{cursor:pointer;color:var(--ink-2);font-weight:500;
  border-bottom:1px dotted var(--axis)}
.examples b:hover{color:var(--ink)}

.status{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:18px;min-height:26px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  border:1px solid var(--ring);border-radius:999px;padding:3px 10px;
  color:var(--ink-2);background:var(--surface)}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.pill.err{border-color:color-mix(in srgb,var(--critical) 40%,transparent);
  color:var(--critical)}
.pill.none{opacity:.6}
/* "Couldn't check" is not a verdict on stock, so it is amber, not red. */
.pill.warn{border-color:color-mix(in srgb,var(--warning) 60%,transparent);
  color:var(--ink-2)}
.why{color:var(--muted);font-size:11.5px;margin-top:2px}
.asof{color:var(--muted);font-size:11px;margin-top:2px}
.caveat{font-size:12.5px;color:var(--ink);line-height:1.45;
  background:color-mix(in srgb,var(--warning) 16%,transparent);
  border:1px solid color-mix(in srgb,var(--warning) 45%,transparent);
  border-radius:9px;padding:8px 11px;margin:0 0 12px}
.spin{display:inline-block;width:11px;height:11px;border-radius:50%;
  border:2px solid var(--axis);border-top-color:var(--ink);
  animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

.card{background:var(--surface);border:1px solid var(--ring);border-radius:13px;
  padding:18px;margin-bottom:16px}
.setline{display:flex;justify-content:space-between;align-items:baseline;
  gap:12px;flex-wrap:wrap;margin-bottom:4px}
.setnum{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12.5px;
  letter-spacing:.04em}
h2{font-size:16px;margin:0;font-weight:600;letter-spacing:-0.01em}
.meta{color:var(--ink-2);font-size:12.5px;margin-bottom:14px}
.spread{color:var(--critical);font-weight:600}

table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);font-weight:600;padding:0 10px 7px;border-bottom:1px solid var(--grid)}
td{padding:10px;border-bottom:1px solid var(--grid);vertical-align:middle}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.price{font-size:15px;font-weight:600}
.cheap{color:var(--good)}
.oos{color:var(--critical);font-size:12px}
.mrp{color:var(--muted);text-decoration:line-through;font-size:12px}
a.shop{color:inherit;text-decoration:none;font-weight:500}
a.shop:hover{text-decoration:underline}
.swatch{width:9px;height:9px;border-radius:2px;display:inline-block;
  margin-right:7px;flex:none}
.ttl{color:var(--muted);font-size:11.5px;margin-top:2px;
  max-width:430px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font-size:11px;font-weight:600;color:var(--good);
  border:1px solid color-mix(in srgb,var(--good) 32%,transparent);
  border-radius:999px;padding:1px 7px;margin-left:7px}
.empty{text-align:center;color:var(--ink-2);padding:40px 10px}
.empty h3{margin:0 0 6px;font-size:15px;color:var(--ink)}
.note{color:var(--muted);font-size:12.5px;margin-top:14px}

/* ---- tabs ---- */
.tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--grid)}
.tab{background:none;border:none;border-bottom:2px solid transparent;
  border-radius:0;padding:9px 14px;color:var(--ink-2);font-weight:500}
.tab:hover{color:var(--ink);border-color:var(--grid)}
.tab[aria-selected="true"]{color:var(--ink);border-color:var(--ink);font-weight:600}

/* ---- browse grid ---- */
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
select,.controls input{font:inherit;padding:7px 10px;border-radius:8px;
  border:1px solid var(--ring);background:var(--surface);color:var(--ink)}
.controls input[type=search]{min-width:190px}
.grid{display:grid;gap:14px;
  grid-template-columns:repeat(auto-fill,minmax(168px,1fr))}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  overflow:hidden;cursor:pointer;display:flex;flex-direction:column;
  transition:border-color .12s, transform .12s}
.tile:hover{border-color:var(--axis);transform:translateY(-2px)}
.tile[aria-expanded="true"]{border-color:var(--ink)}
/* Product shots are transparent PNGs shot on white. On a dark surface they
   turn into a dark smear, so the thumbnail keeps a light ground in BOTH
   themes — this is a photographic plate, not chrome. */
.thumb{aspect-ratio:1/1;background:#ffffff;display:flex;
  align-items:center;justify-content:center;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:contain;padding:8px}
/* A dead image URL must not spill its alt text across the tile. */
.thumb img[data-broken]{display:none}
.thumb .ph{color:#8a8a8a;font-size:11px;text-align:center;padding:0 8px}
.tbody{padding:10px 11px 12px;display:flex;flex-direction:column;gap:3px;flex:1}
.tset{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;
  letter-spacing:.04em}
.tname{font-size:12.5px;line-height:1.35;font-weight:500;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tmeta{font-size:11px;color:var(--muted);margin-top:auto;padding-top:6px}
.tprice{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums;
  display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.tprice .was{font-size:11.5px;color:var(--muted);text-decoration:line-through;
  font-weight:400}
.tprice .off{font-size:11.5px;font-weight:600;color:var(--good)}
.twho{font-size:11.5px;color:var(--ink-2);display:flex;align-items:center;
  gap:5px;margin-top:1px}
.tppp{font-size:12px;font-weight:600;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
.tstale{font-size:10.5px;color:var(--muted)}
.oos-tag{color:var(--critical);font-size:11px}
.tile.dim .thumb{opacity:.55}
.tile.dim .tname{color:var(--ink-2)}
.rowstate{color:var(--muted);font-size:12px}
.more{display:block;margin:20px auto 0;padding:9px 20px}

/* ---- expanded comparison ---- */
.compare{grid-column:1/-1;background:var(--surface);border:1px solid var(--ink);
  border-radius:12px;padding:16px}
.chead{display:flex;gap:14px;align-items:flex-start;margin-bottom:12px;flex-wrap:wrap}
.chead img{width:74px;height:74px;object-fit:contain;background:#ffffff;
  border-radius:9px;padding:5px;flex:none}
.ctitle{font-size:15px;font-weight:600;margin:0 0 3px}
.spin-row{display:flex;align-items:center;gap:8px;color:var(--ink-2);
  font-size:13px;padding:14px 2px}
</style></head>
<body><div class="wrap">
<header>
  <div><h1>LEGO Search — India</h1>
    <div class="sub">Live prices from seven retailers, fetched when you ask.</div></div>
  <button id="theme">Dark mode</button>
</header>

<div class="tabs" role="tablist">
  <button class="tab" id="tab-search" role="tab" aria-selected="true">Search</button>
  <button class="tab" id="tab-browse" role="tab" aria-selected="false">Browse catalogue</button>
</div>

<div id="view-browse" hidden>
  <div class="controls">
    <input type="search" id="bq" placeholder="Filter by name or set number">
    <select id="btheme"><option value="">All themes</option></select>
    <select id="bsort">
      <option value="name">Name A–Z</option>
      <option value="price_asc">Price: low to high</option>
      <option value="price_desc">Price: high to low</option>
      <option value="pieces_desc">Most pieces</option>
      <option value="ppp_asc">₹ per piece: lowest first</option>
      <option value="ppp_desc">₹ per piece: highest first</option>
    </select>
    <label style="font-size:12.5px;color:var(--ink-2);display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="binstock"> In stock only</label>
    <button id="brefresh">Refresh catalogue</button>
  </div>
  <div class="grid" id="bgrid"></div>
  <button class="more" id="bmore" hidden>Show more</button>
  <p class="note" id="bnote"></p>
</div>

<div id="view-search">
<form id="f" autocomplete="off">
  <input id="q" type="search" placeholder="Set number (71806) or name (millennium falcon)"
         aria-label="Search for a LEGO set" autofocus>
  <button id="go" type="submit">Search</button>
</form>
<div class="examples">Try
  <b data-q="71806">71806</b> ·
  <b data-q="75375">75375</b> ·
  <b data-q="millennium falcon">millennium falcon</b> ·
  <b data-q="ninjago city">ninjago city</b>
</div>

<div class="status" id="status"></div>
<div id="results"></div>
</div>
</div>

<script>
const $ = s => document.querySelector(s);
const RET = ["amazon_in","flipkart","firstcry","hamleys","jaimantoys",
             "mybrickhouse","toycra"];
const LBL = {amazon_in:"Amazon.in",flipkart:"Flipkart",firstcry:"FirstCry",
             hamleys:"Hamleys",jaimantoys:"Jaiman Toys",
             mybrickhouse:"LEGO Certified Store",toycra:"Toycra"};
const inr = n => n==null ? "—" : "₹"+Math.round(n).toLocaleString("en-IN");
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* A retailer whose request failed was NOT checked. It must never read as
   unavailable or out of stock, and while one is missing the cheapest price
   we show is only the cheapest we could see. */
const failedOf = d => (d.statuses || []).filter(s => s.error);
const joinNames = xs => xs.length < 2 ? xs.join("")
  : xs.slice(0,-1).join(", ") + " and " + xs[xs.length-1];
function caveat(failed){
  if (!failed.length) return "";
  const who = joinNames(failed.map(s => s.label));
  const why = failed.length === 1 && failed[0].reason ? ` (${failed[0].reason})` : "";
  return `<div class="caveat"><b>Couldn't check ${esc(who)}</b>${esc(why)}, so the
    real cheapest price may be there. This is not a stock status.</div>`;
}
const agoShort = s => s < 3600 ? `${Math.max(1, Math.round(s/60))} min ago`
                               : `${Math.round(s/3600)} h ago`;

/* theme */
const tb = $("#theme");
if (matchMedia("(prefers-color-scheme: dark)").matches){
  document.documentElement.setAttribute("data-theme","dark"); tb.textContent="Light mode";
}
tb.onclick = () => {
  const dark = document.documentElement.getAttribute("data-theme")==="dark";
  document.documentElement.setAttribute("data-theme", dark?"light":"dark");
  tb.textContent = dark?"Dark mode":"Light mode";
};

document.querySelectorAll(".examples b").forEach(b=>b.onclick=()=>{
  $("#q").value=b.dataset.q; run(b.dataset.q);
});

$("#f").onsubmit = e => { e.preventDefault(); run($("#q").value); };

let inflight = null;

async function run(q){
  q = (q||"").trim();
  if(!q) return;
  history.replaceState({},"","?q="+encodeURIComponent(q));
  document.title = q + " — LEGO Search";

  $("#status").innerHTML = RET.map(r =>
    `<span class="pill"><span class="spin"></span>${LBL[r]}</span>`).join("");
  $("#results").innerHTML = "";

  if (inflight) inflight.abort();
  inflight = new AbortController();
  let data;
  try{
    const res = await fetch("/api/search?q="+encodeURIComponent(q),
                            {signal: inflight.signal});
    data = await res.json();
    if (data.error) throw new Error(data.error);
  }catch(err){
    if (err.name === "AbortError") return;
    $("#status").innerHTML = "";
    $("#results").innerHTML =
      `<div class="card empty"><h3>Search failed</h3><div>${esc(err.message)}</div></div>`;
    return;
  }
  render(data);
}

function render(d){
  $("#status").innerHTML = d.statuses.map(s=>{
    const cls = s.error ? "pill warn" : (s.matched ? "pill" : "pill none");
    const dot = s.error ? "var(--warning)" : `var(--${s.retailer})`;
    const txt = s.error ? "couldn't check"
              : (s.matched ? s.matched+" found" : "no match");
    const tip = s.error ? (s.reason || "") + " — " + s.error
              : (s.cached_age_s != null ? `saved answer from ${agoShort(s.cached_age_s)}`
                                        : s.returned+' raw hits in '+s.ms+'ms');
    return `<span class="${cls}" title="${esc(tip)}">
      <span class="dot" style="background:${dot}"></span>${s.label} · ${txt}</span>`;
  }).join("");

  const failed = failedOf(d);
  if(!d.sets.length){
    const reached = failed.length
      ? `${7 - failed.length} of 7 retailers were reached (${esc(joinNames(failed.map(s=>s.label)))} couldn't be checked)`
      : "Every retailer was reached";
    $("#results").innerHTML = `<div class="card empty">
      <h3>No confident match for “${esc(d.query)}”</h3>
      <div>${reached}${d.rejected? `, and ${d.rejected} fuzzy
        result${d.rejected===1?" was":"s were"} rejected as the wrong set` : ""}.
        Try the exact set number — it is printed on the box.</div></div>`;
    return;
  }
  const bestLabel = failed.length ? "cheapest found" : "cheapest";

  $("#results").innerHTML = d.sets.map(s=>{
    const rows = s.offers.map(o=>{
      const isBest = o.retailer === s.best && o.price != null;
      return `<tr>
        <td><span class="swatch" style="background:var(--${o.retailer})"></span>
            <a class="shop" href="${esc(o.url)}" target="_blank" rel="noopener">${o.label}</a>
            ${isBest?`<span class="badge">${bestLabel}</span>`:""}
            <div class="ttl">${esc(o.title)}</div></td>
        <td class="num">${o.mrp? `<span class="mrp">${inr(o.mrp)}</span>`:""}</td>
        <td class="num"><span class="price ${isBest?"cheap":""}">${inr(o.price)}</span>
            ${o.in_stock===false?'<div class="oos">out of stock</div>':""}</td>
      </tr>`;
    }).join("");

    const n = s.offers.filter(o=>o.price!=null).length;
    return `<div class="card">
      <div class="setline"><h2>${esc(s.name)}</h2><span class="setnum">SET ${s.set_num}</span></div>
      <div class="meta">${n} retailer${n===1?"":"s"} carry this${
        s.spread!=null ? ` · <span class="spread">${s.spread}% spread</span> between cheapest and dearest` : ""}</div>
      ${caveat(failed)}
      <table><thead><tr><th>Retailer</th><th class="num">MRP</th><th class="num">Price</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>`;
  }).join("") + `<div class="note">Searched in ${(d.elapsed_ms/1000).toFixed(1)}s.
    ${d.rejected} fuzzy result${d.rejected===1?"":"s"} rejected as the wrong set —
    retailer search engines match loosely, so listings are verified by set number
    before being shown.</div>`;
}

/* ================= Browse catalogue ================= */
let CAT = null, catLoaded = false, shown = 60, openSet = null;

const tabS = $("#tab-search"), tabB = $("#tab-browse");
function showTab(which){
  const browse = which === "browse";
  tabB.setAttribute("aria-selected", String(browse));
  tabS.setAttribute("aria-selected", String(!browse));
  $("#view-browse").hidden = !browse;
  $("#view-search").hidden = browse;
  if (browse && !catLoaded) loadCatalog();
}
tabS.onclick = () => showTab("search");
tabB.onclick = () => showTab("browse");

async function loadCatalog(force){
  catLoaded = true;
  $("#bgrid").innerHTML =
    `<div class="spin-row" style="grid-column:1/-1"><span class="spin"></span>
     ${force ? "Refreshing" : "Loading"} the catalogue from the LEGO Certified Store…</div>`;
  try{
    const res = await fetch(force ? "/api/catalog/refresh" : "/api/catalog");
    const d = await res.json();
    if (d.error) throw new Error(d.error);
    if (force){ catLoaded = false; return loadCatalog(false); }
    CAT = d;
    const sel = $("#btheme");
    sel.innerHTML = `<option value="">All themes</option>` +
      d.themes.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join("");
    drawGrid();
  }catch(err){
    $("#bgrid").innerHTML =
      `<div class="empty" style="grid-column:1/-1"><h3>Could not load the catalogue</h3>
       <div>${esc(err.message)}</div></div>`;
  }
}

function catFiltered(){
  if (!CAT) return [];
  let rows = CAT.items.slice();
  const q = $("#bq").value.trim().toLowerCase();
  const theme = $("#btheme").value;
  if (q) rows = rows.filter(r => (r.set_num + " " + r.name).toLowerCase().includes(q));
  if (theme) rows = rows.filter(r => r.theme === theme);
  if ($("#binstock").checked) rows = rows.filter(r => r.in_stock);

  const px = r => r.show_price;
  const by = {
    name: (a,b) => (a.display_name||a.name).localeCompare(b.display_name||b.name),
    price_asc: (a,b) => (px(a) ?? Infinity) - (px(b) ?? Infinity),
    price_desc: (a,b) => (px(b) ?? -1) - (px(a) ?? -1),
    pieces_desc: (a,b) => (b.pieces ?? -1) - (a.pieces ?? -1),
    ppp_asc: (a,b) => (a.per_piece ?? Infinity) - (b.per_piece ?? Infinity),
    ppp_desc: (a,b) => (b.per_piece ?? -1) - (a.per_piece ?? -1),
  }[$("#bsort").value];

  // Sets that are out of stock at every retailer we have checked sink to the
  // bottom whatever the sort. A set nobody has priced yet is *unknown*, not
  // unavailable, so it is not demoted.
  return rows.sort((a,b) => (a.out_everywhere?1:0) - (b.out_everywhere?1:0)
                            || by(a,b));
}

function ago(iso){
  if (!iso) return "";
  const d = (Date.now() - Date.parse(iso)) / 86400000;
  if (!isFinite(d)) return "";
  if (d < 1) return "today";
  if (d < 2) return "1d ago";
  return `${Math.floor(d)}d ago`;
}

function drawGrid(){
  const rows = catFiltered();
  const page = rows.slice(0, shown);
  $("#bgrid").innerHTML = page.map(r => {
    const b = r.best;
    const who = b ? b.retailer : "mybrickhouse";
    const whoLabel = b ? LBL[b.retailer] : "LEGO Certified Store";
    const pieces = r.pieces ? `${r.pieces.toLocaleString("en-IN")} pcs` : "";

    return `<div class="tile${r.out_everywhere?" dim":""}"
                 data-set="${r.set_num}" aria-expanded="false">
      <div class="thumb">${r.image
        ? `<img src="${esc(r.image)}" alt="" loading="lazy"
             onerror="this.dataset.broken=1;this.insertAdjacentHTML('afterend',
               '<span class=&quot;ph&quot;>no image</span>')">`
        : `<span class="ph">no image</span>`}</div>
      <div class="tbody">
        <div class="tset">#${r.set_num}</div>
        <div class="tname">${esc(r.display_name || r.name)}</div>

        <div class="tmeta">${pieces || "&nbsp;"}</div>
        <div class="tprice">${r.show_price==null ? "—" : inr(r.show_price)}
          ${r.show_mrp ? `<span class="was">${inr(r.show_mrp)}</span>` : ""}
          ${r.discount_pct ? `<span class="off">${r.discount_pct}% off</span>` : ""}</div>
        ${r.per_piece!=null
          ? `<div class="tppp">₹${r.per_piece.toFixed(2)} per piece</div>` : ""}
        <div class="twho"><span class="swatch" style="background:var(--${who})"></span>
          ${whoLabel}${b ? "" : " only"}</div>
        ${b ? `<div class="tstale">cheapest of ${b.n} checked · ${ago(b.at)}</div>`
            : `<div class="tstale">not compared yet — click to check all seven</div>`}
        ${r.out_everywhere
          ? `<div class="oos-tag">out of stock everywhere checked</div>`
          : (r.in_stock ? "" : `<div class="oos-tag">out at the Certified Store</div>`)}
      </div></div>`;
  }).join("") || `<div class="empty" style="grid-column:1/-1">
      <h3>Nothing matches those filters</h3></div>`;

  $("#bmore").hidden = rows.length <= shown;
  $("#bmore").textContent = `Show more (${rows.length - shown} left)`;
  const when = CAT?.refreshed_at ? CAT.refreshed_at.slice(0,10) : "unknown";
  $("#bnote").textContent =
    `${Math.min(shown, rows.length)} of ${rows.length} sets · catalogue from the `
    + `LEGO Certified Store, last refreshed ${when}. `
    + `The price shown is theirs — click a set to compare all seven retailers live.`;
  openSet = null;
}

$("#bq").oninput = () => { shown = 60; drawGrid(); };
$("#btheme").onchange = () => { shown = 60; drawGrid(); };
$("#bsort").onchange = () => { shown = 60; drawGrid(); };
$("#binstock").onchange = () => { shown = 60; drawGrid(); };
$("#bmore").onclick = () => { shown += 60; drawGrid(); };
$("#brefresh").onclick = () => { catLoaded = false; shown = 60; loadCatalog(true); };

$("#bgrid").addEventListener("click", async ev => {
  const tile = ev.target.closest(".tile");
  if (!tile) return;
  const setNum = tile.dataset.set;

  document.querySelectorAll(".compare").forEach(n => n.remove());
  document.querySelectorAll('.tile[aria-expanded="true"]')
    .forEach(t => t.setAttribute("aria-expanded","false"));
  if (openSet === setNum){ openSet = null; return; }
  openSet = setNum;
  tile.setAttribute("aria-expanded","true");

  const item = CAT.items.find(i => i.set_num === setNum);
  const panel = document.createElement("div");
  panel.className = "compare";
  panel.innerHTML = `<div class="chead">
      ${item.image ? `<img src="${esc(item.image)}" alt="">` : ""}
      <div><div class="ctitle">${esc(item.display_name || item.name)}</div>
        <div style="color:var(--ink-2);font-size:12.5px">#${setNum}${
          item.pieces ? " · " + item.pieces.toLocaleString("en-IN") + " pieces" : ""}</div>
      </div></div>
    <div class="spin-row"><span class="spin"></span>Checking all seven retailers…</div>`;
  tile.after(panel);
  panel.scrollIntoView({behavior:"smooth", block:"nearest"});

  try{
    const d = await (await fetch("/api/search?q="+encodeURIComponent(setNum))).json();
    if (d.error) throw new Error(d.error);
    const set = d.sets.find(s => s.set_num === setNum);
    const offers = set ? set.offers : [];
    const byRet = {};
    offers.forEach(o => { byRet[o.retailer] = o; });
    const statusOf = {};
    (d.statuses || []).forEach(s => { statusOf[s.retailer] = s; });
    const failed = failedOf(d);
    const bestLabel = failed.length ? "cheapest found" : "cheapest";

    // Always show all seven. A retailer with no verified listing is a real
    // answer ("doesn't stock it") and belongs in the table; hiding it made the
    // comparison look like only one or two shops exist.
    const priced = RET.filter(r => byRet[r] && byRet[r].price != null)
                      .sort((a,b) => byRet[a].price - byRet[b].price);
    const rest = RET.filter(r => !priced.includes(r));

    const rows = priced.concat(rest).map(r => {
      const o = byRet[r];
      const st = statusOf[r];
      const best = set && r === set.best && o && o.price != null;
      let right;
      if (o && o.price != null){
        const age = st && st.cached_age_s != null && st.cached_age_s >= 60
          ? `<div class="asof">as of ${agoShort(st.cached_age_s)}</div>` : "";
        right = `<span class="price ${best?"cheap":""}">${inr(o.price)}</span>
                 ${o.in_stock===false?'<div class="oos">out of stock</div>':""}${age}`;
      } else if (st && st.error){
        right = `<span class="rowstate" title="${esc(st.error)}">couldn't check</span>
                 <div class="why">${esc(st.reason || "")}</div>`;
      } else {
        right = `<span class="rowstate">doesn't stock it</span>`;
      }
      const name = o
        ? `<a class="shop" href="${esc(o.url)}" target="_blank" rel="noopener">${LBL[r]}</a>`
        : `<span style="color:var(--ink-2)">${LBL[r]}</span>`;
      return `<tr>
        <td><span class="swatch" style="background:var(--${r})"></span>${name}
          ${best?`<span class="badge">${bestLabel}</span>`:""}</td>
        <td class="num">${o && o.mrp?`<span class="mrp">${inr(o.mrp)}</span>`:""}</td>
        <td class="num">${right}</td></tr>`;
    }).join("");

    const checked = 7 - failed.length;
    const head = (priced.length === 0
      ? `<div class="meta" style="margin-bottom:8px;color:var(--critical)">
           None of the ${checked} retailers we could check has a verified listing</div>`
      : (set && set.spread != null
          ? `<div class="meta" style="margin-bottom:8px">
               <span class="spread">${set.spread}% spread</span>
               across ${priced.length} retailers that stock it</div>`
          : `<div class="meta" style="margin-bottom:8px">
               only 1 of the ${checked} retailers we could check stocks it</div>`))
      + caveat(failed);

    panel.querySelector(".spin-row").outerHTML = head +
      `<table><thead><tr><th>Retailer</th><th class="num">MRP</th>
       <th class="num">Price</th></tr></thead><tbody>${rows}</tbody></table>
       <p class="note">${failed.length ? `${checked} of 7` : "All seven"} checked in ${(d.elapsed_ms/1000).toFixed(1)}s`
      + (d.rejected ? ` · ${d.rejected} fuzzy result${d.rejected===1?"":"s"} rejected as the wrong set` : "")
      + `.</p>`;
    // Refresh the tile so it picks up the price we just learned.
    fetch("/api/catalog?refresh=0").then(r=>r.json()).then(nd => { if (nd.items) CAT = nd; });
  }catch(err){
    panel.querySelector(".spin-row").outerHTML =
      `<div class="empty"><h3>Comparison failed</h3><div>${esc(err.message)}</div></div>`;
  }
});

const params = new URLSearchParams(location.search);
const initial = params.get("q");
if (initial){ $("#q").value = initial; run(initial); }
if (params.get("tab") === "browse") showTab("browse");
</script>
</body></html>
"""
