/* Search and browse filtering. No framework, no build step, ~4 KB.
 *
 * Everything here is progressive: the browse grid is fully rendered in HTML and
 * this only reorders and hides it, so the catalogue still works with JavaScript
 * off. The home page search does need JS — but it fetches a static JSON file,
 * not an API, so there is no server that can be down.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ helpers */

  function rupees(n) {
    if (n === null || n === undefined || n === "") return "—";
    var whole = String(Math.round(Number(n)));
    if (whole.length > 3) {
      var head = whole.slice(0, -3), tail = whole.slice(-3), parts = [];
      while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) parts.unshift(head);
      whole = parts.join(",") + "," + tail;
    }
    return "₹" + whole;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    // Always textContent. Set names come from retailer listings, so nothing
    // from the search index is ever parsed as markup. The test suite greps
    // this file to make sure that stays true.
    if (text !== undefined) n.textContent = text;
    return n;
  }

  /* ------------------------------------------------------- home search */

  var form = document.getElementById("searchform");
  if (form) {
    var input = document.getElementById("q");
    var out = document.getElementById("results");
    var hint = document.getElementById("hint");
    var index = null;
    var loading = false;

    function load() {
      if (index || loading) return Promise.resolve(index);
      loading = true;
      return fetch("search-index.json")
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (data) { index = data; loading = false; return data; })
        .catch(function () {
          loading = false;
          out.replaceChildren(el("div", "nohit",
            "Couldn't load the set list. Try reloading the page."));
          return null;
        });
    }

    // Ask for the index as soon as someone shows intent, so the first
    // keystroke doesn't wait on the network.
    input.addEventListener("focus", load, { once: true });

    function score(row, q, isNum) {
      var num = row.n, name = (row.t || "").toLowerCase();
      if (isNum) {
        if (num === q) return 0;                 // exact set number wins outright
        if (num.indexOf(q) === 0) return 1;
        if (num.indexOf(q) > -1) return 2;
        return name.indexOf(q) > -1 ? 3 : -1;
      }
      if (name.indexOf(q) === 0) return 0;
      var word = name.indexOf(" " + q);
      if (word > -1) return 1;
      if (name.indexOf(q) > -1) return 2;
      if ((row.h || "").toLowerCase().indexOf(q) > -1) return 3;
      return -1;
    }

    function render(q) {
      q = q.trim().toLowerCase();
      if (!q) { out.replaceChildren(); return; }
      if (!index) { load().then(function (d) { if (d) render(q); }); return; }

      var isNum = /^\d+$/.test(q);
      var hits = [];
      for (var i = 0; i < index.length; i++) {
        var s = score(index[i], q, isNum);
        if (s > -1) hits.push({ row: index[i], s: s });
      }
      hits.sort(function (a, b) {
        return a.s - b.s || (a.row.t || "").localeCompare(b.row.t || "");
      });

      out.replaceChildren();
      if (!hits.length) {
        var msg = el("div", "nohit");
        msg.appendChild(document.createTextNode(
          "Nothing matching “" + q + "”. "));
        msg.appendChild(document.createTextNode(
          isNum
            ? "That set may not be sold in India, or we may not track it yet."
            : "Try the set number instead — it's on the front of the box."));
        out.appendChild(msg);
        return;
      }

      hits.slice(0, 12).forEach(function (h) {
        var r = h.row;
        var a = el("a", "hit");
        a.href = "set/" + r.n + "/";
        a.appendChild(el("span", "hname", r.t));
        a.appendChild(el("span", "hnum", "#" + r.n + (r.h ? " · " + r.h : "")));
        a.appendChild(el("span", "hprice", rupees(r.p)));
        a.appendChild(el("span", "hwhere", r.p ? (r.r || "") : "no price on record"));
        out.appendChild(a);
      });

      if (hint) {
        hint.textContent = hits.length + " match" +
          (hits.length === 1 ? "" : "es") + " · showing " +
          Math.min(12, hits.length);
      }
    }

    var timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var v = input.value;
      timer = setTimeout(function () { render(v); }, 90);
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var q = input.value.trim();
      // A bare set number is unambiguous: go straight there.
      if (/^\d{4,7}$/.test(q) && index) {
        for (var i = 0; i < index.length; i++) {
          if (index[i].n === q) { window.location.href = "set/" + q + "/"; return; }
        }
      }
      render(q);
    });

    // Support /?q=42176 so links into the site can pre-fill the search.
    var params = new URLSearchParams(window.location.search);
    var preset = params.get("q");
    if (preset) {
      input.value = preset;
      load().then(function (d) { if (d) render(preset); });
    }
  }

  /* ----------------------------------------------------- browse filter */

  var grid = document.getElementById("tiles");
  if (grid) {
    var bq = document.getElementById("bq");
    var btheme = document.getElementById("btheme");
    var bsort = document.getElementById("bsort");
    var bstock = document.getElementById("bstock");
    var bcount = document.getElementById("bcount");
    var bempty = document.getElementById("bempty");
    var tiles = Array.prototype.slice.call(grid.children);

    var data = tiles.map(function (node) {
      return {
        node: node,
        name: node.dataset.name || "",
        num: node.dataset.num || "",
        theme: node.dataset.theme || "",
        price: parseFloat(node.dataset.price) || null,
        pieces: parseInt(node.dataset.pieces, 10) || null,
        per: parseFloat(node.dataset.per) || null,
        out: node.dataset.out === "1"
      };
    });

    // Sets with no price are "unknown", not "expensive" — they sort to the end
    // of a price sort rather than to the top, whichever direction you pick.
    function cmp(mode) {
      return function (a, b) {
        function nulls(x) { return x === null || isNaN(x); }
        switch (mode) {
          case "price":
            if (nulls(a.price) !== nulls(b.price)) return nulls(a.price) ? 1 : -1;
            return (a.price - b.price) || a.name.localeCompare(b.name);
          case "price-desc":
            if (nulls(a.price) !== nulls(b.price)) return nulls(a.price) ? 1 : -1;
            return (b.price - a.price) || a.name.localeCompare(b.name);
          case "per":
            if (nulls(a.per) !== nulls(b.per)) return nulls(a.per) ? 1 : -1;
            return (a.per - b.per) || a.name.localeCompare(b.name);
          case "pieces-desc":
            if (nulls(a.pieces) !== nulls(b.pieces)) return nulls(a.pieces) ? 1 : -1;
            return (b.pieces - a.pieces) || a.name.localeCompare(b.name);
          default:
            return a.name.localeCompare(b.name);
        }
      };
    }

    function apply() {
      var q = (bq.value || "").trim().toLowerCase();
      var theme = btheme.value;
      var stockOnly = bstock.checked;

      var shown = data.filter(function (d) {
        if (theme && d.theme !== theme) return false;
        if (stockOnly && d.out) return false;
        if (q && d.name.indexOf(q) === -1 && d.num.indexOf(q) === -1) return false;
        return true;
      });

      // Out-of-stock sinks within whatever sort is active — but a set nobody
      // has checked is not "out", so it keeps its place.
      shown.sort(cmp(bsort.value));
      shown.sort(function (a, b) { return (a.out ? 1 : 0) - (b.out ? 1 : 0); });

      var frag = document.createDocumentFragment();
      shown.forEach(function (d) { frag.appendChild(d.node); });
      data.forEach(function (d) {
        if (shown.indexOf(d) === -1 && d.node.parentNode) d.node.remove();
      });
      grid.replaceChildren(frag);

      bcount.textContent = shown.length === data.length
        ? data.length + " sets"
        : shown.length + " of " + data.length + " sets";
      bempty.hidden = shown.length > 0;
    }

    var btimer = null;
    bq.addEventListener("input", function () {
      clearTimeout(btimer);
      btimer = setTimeout(apply, 110);
    });
    [btheme, bsort, bstock].forEach(function (c) {
      c.addEventListener("change", apply);
    });
    apply();
  }
})();
