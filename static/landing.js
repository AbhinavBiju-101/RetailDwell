/* Landing deck: slide navigation, the hero heat-map illustration and the small interactive explainers.
   The hero maps are drawn procedurally from made-up blobs. Nothing here is measured data. */
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  /* ------------------------------------------------------------------ hero heat maps */
  const W = 800, H = 480, SW = 16, SH = 9.6;
  const BEFORE = [[2, 4.8, 1.4, 0.55], [8, 2, 2.4, 0.45], [8, 7.5, 2.2, 0.4], [13.2, 8.2, 1.5, 0.75], [13.4, 5.6, 1.1, 0.35], [4.5, 2, 1.2, 0.35], [13.6, 2, 1.3, 0.22]];
  const AFTER = [[2, 4.8, 1.4, 0.55], [8, 2, 2.4, 0.4], [8, 7.5, 2.2, 0.35], [13.2, 8.2, 1.5, 0.7], [7.6, 4.8, 1.7, 0.95], [4.5, 2, 1.2, 0.3]];
  const ZONES = [["Entrance", 0, 3.6, 1.2, 6], ["Offers", 12.2, 0.6, 15.4, 3.2], ["Offers", 5.9, 3.9, 9.3, 5.9], ["Tills", 12, 7.4, 15.4, 9.2], ["Produce", 3.4, 0.6, 9.8, 3.0], ["Dairy", 3.4, 6.8, 9.8, 9.0]];
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

  function field(blobs) {
    const g = new Float32Array(W * H), kx = W / SW, ky = H / SH;
    blobs.forEach(([bx, by, sp, wt]) => {
      const r = sp * 3;
      for (let y = Math.max(0, Math.floor((by - r) * ky)); y < Math.min(H, Math.ceil((by + r) * ky)); y++) {
        for (let x = Math.max(0, Math.floor((bx - r) * kx)); x < Math.min(W, Math.ceil((bx + r) * kx)); x++) {
          const dx = x / kx - bx, dy = y / ky - by;
          g[y * W + x] += wt * Math.exp(-(dx * dx + dy * dy) / (2 * sp * sp));
        }
      }
    });
    return g;
  }
  let fa, fb, fmax = 1;

  function paint(id, f, skip) {
    const cv = document.getElementById(id);
    if (!cv || !window.App) return;
    const stops = App.stops().map(([p, c]) => [p, hex(c)]);
    const g = App.gamma();
    // lookup table of 256 colours so painting stays fast while sliders move
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const v = i / 255;
      let k = 1; while (k < stops.length - 1 && v > stops[k][0]) k++;
      const t = (v - stops[k - 1][0]) / (stops[k][0] - stops[k - 1][0]);
      for (let c = 0; c < 3; c++) lut[i * 3 + c] = stops[k - 1][1][c] + (stops[k][1][c] - stops[k - 1][1][c]) * t;
    }
    const ctx = cv.getContext("2d"), img = ctx.createImageData(W, H);
    for (let i = 0; i < f.length; i++) {
      const idx = Math.round(Math.pow(Math.min(f[i] / fmax, 1), g) * 255);
      img.data[i * 4] = lut[idx * 3]; img.data[i * 4 + 1] = lut[idx * 3 + 1]; img.data[i * 4 + 2] = lut[idx * 3 + 2]; img.data[i * 4 + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    const kx = W / SW, ky = H / SH;
    ctx.lineWidth = 2; ctx.strokeStyle = "rgba(255,255,255,.85)";
    ctx.font = "600 15px 'Source Sans 3', system-ui, sans-serif";
    ZONES.forEach(([name, x0, y0, x1, y1], i) => {
      if (i === skip) return;                       // the offers display is drawn once per layout
      ctx.strokeRect(x0 * kx, y0 * ky, (x1 - x0) * kx, (y1 - y0) * ky);
      const w = ctx.measureText(name).width + 10;
      ctx.fillStyle = "rgba(10,18,30,.66)"; ctx.fillRect(x0 * kx + 2, y0 * ky + 2, w, 22);
      ctx.fillStyle = "#fff"; ctx.fillText(name, x0 * kx + 7, y0 * ky + 18);
    });
  }
  function drawHero() { paint("lp-a", fa, 2); paint("lp-b", fb, 1); }

  function initHero() {
    const wipe = $("#lp-wipe"), range = $("#lp-range");
    if (!wipe) return;
    fa = field(BEFORE); fb = field(AFTER);
    for (let i = 0; i < fa.length; i++) fmax = Math.max(fmax, fa[i], fb[i]);
    range.addEventListener("input", () => wipe.style.setProperty("--p", range.value + "%"));
    drawHero();
    document.addEventListener("heatchange", drawHero);
  }

  /* ------------------------------------------------------------------ deck navigation */
  function initDeck() {
    const slides = $$(".slide"), dotsBox = $("#deck-dots"), bar = $("#deck-bar");
    slides.forEach((s, i) => {
      const a = document.createElement("a");
      a.href = "#" + s.id; a.setAttribute("aria-label", "Slide " + (i + 1) + ": " + s.dataset.title);
      a.innerHTML = "<span>" + s.dataset.title + "</span>";
      dotsBox.appendChild(a);
    });
    const dots = $$("a", dotsBox);
    let current = 0;
    const setCurrent = (i) => { current = i; dots.forEach((d, k) => d.setAttribute("aria-current", String(k === i))); };
    setCurrent(0);
    slides[0].classList.add("in");

    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); setCurrent(slides.indexOf(e.target)); } });
    }, { threshold: 0.45 });
    slides.forEach((s) => io.observe(s));
    // tall slides on small screens may never reach 45% visible: reveal them on a lower threshold too
    const io2 = new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) e.target.classList.add("in"); }), { threshold: 0.12 });
    slides.forEach((s) => io2.observe(s));

    const onScroll = () => {
      const max = document.documentElement.scrollHeight - innerHeight;
      bar.style.width = (max > 0 ? Math.min(100, (scrollY / max) * 100) : 0) + "%";
    };
    addEventListener("scroll", onScroll, { passive: true }); onScroll();

    const go = (i) => { i = Math.max(0, Math.min(slides.length - 1, i)); slides[i].scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }); };
    addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target, tag = t && t.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if ((e.key === " " || e.key === "Enter") && (tag === "BUTTON" || tag === "A" || tag === "TR" || tag === "LI" || tag === "ARTICLE")) return;
      if (e.key === "ArrowDown" || e.key === "PageDown" || (e.key === " " && !e.shiftKey)) { e.preventDefault(); go(current + 1); }
      else if (e.key === "ArrowUp" || e.key === "PageUp" || (e.key === " " && e.shiftKey)) { e.preventDefault(); go(current - 1); }
      else if (e.key === "Home") { e.preventDefault(); go(0); }
      else if (e.key === "End") { e.preventDefault(); go(slides.length - 1); }
    });
  }

  /* ------------------------------------------------------------------ explainers */
  function initFork() {
    const btns = $$("[data-fork]"), panes = $$("[data-fd]");
    btns.forEach((b) => b.addEventListener("click", () => {
      btns.forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      panes.forEach((p) => { p.hidden = p.dataset.fd !== b.dataset.fork; });
    }));
  }

  function initWalker() {
    const w = $("#walker"), items = $$("#obs li");
    if (!w) return;
    const set = (k) => { w.dataset.focus = k; items.forEach((i) => i.classList.toggle("on", i.dataset.o === k)); };
    items.forEach((li) => {
      ["mouseenter", "focus", "click"].forEach((ev) => li.addEventListener(ev, () => set(li.dataset.o)));
    });
    set("where");
  }

  function initTable() {
    const rows = $$("#cmp tbody tr"), out = $("#cmp-why");
    const show = (r) => { rows.forEach((x) => x.classList.toggle("on", x === r)); out.textContent = r.dataset.why; };
    rows.forEach((r) => ["mouseenter", "focus", "click"].forEach((ev) => r.addEventListener(ev, () => show(r))));
  }

  function initShred() {
    const box = $("#shred");
    if (!box) return;
    const note = $("#shred-note"), btns = $$("[data-set]", box);
    const msg = { frame: "The agent detects people in each frame.", kept: "Only four numbers per detection are written. The frame itself is discarded and never stored." };
    btns.forEach((b) => b.addEventListener("click", () => {
      box.dataset.state = b.dataset.set;
      btns.forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      note.textContent = msg[b.dataset.set];
    }));
  }

  function initFeed() {
    const read = $("#feed-read");
    const info = { a: "ID 14 at x 6.2 m, y 7.4 m", b: "ID 15 at x 9.6 m, y 8.6 m", c: "ID 12 at x 3.9 m, y 4.2 m", d: "ID 16 at x 12.1 m, y 3.8 m" };
    const base = read ? read.textContent : "";
    const set = (p) => {
      $$(".pbox, .pdot").forEach((el) => el.classList.toggle("on", !!p && el.dataset.p === p));
      if (read) read.textContent = p ? info[p] + ". Only this row is stored." : base;
    };
    $$(".pbox").forEach((el) => {
      ["mouseenter", "focus"].forEach((ev) => el.addEventListener(ev, () => set(el.dataset.p)));
      ["mouseleave", "blur"].forEach((ev) => el.addEventListener(ev, () => set(null)));
    });
    $$(".pdot").forEach((el) => {
      el.addEventListener("mouseenter", () => set(el.dataset.p));
      el.addEventListener("mouseleave", () => set(null));
    });
  }

  function initStages() {
    const STAGES = [
      ["Sample the stream at 2 to 5 frames per second: about 1 when the store is empty, more when busy. Frames are skipped by whether a person is present, never by pixel change.",
        "A shopper standing still at a display barely changes pixels, and those are the frames that carry dwell. At low rates a walker moves about 0.6 m between frames, so the tracker runs near 8 to 10 fps or the ID-switch rate is reported."],
      ["A small detector finds people and a tracker gives each an anonymous, short-lived number. Short gaps after someone is hidden are stitched when the area and velocity match.",
        "Staff at counters create fake dwell, so staff-only zones are excluded and implausibly long tracks are flagged. A family of four is four tracks: dwell counts people, and conversion would count parties."],
      ["A homography turns the bottom-centre of each box into a position on your floor plan. Draw the plan yourself, or measure four floor points the camera can see.",
        "Wide-angle lenses, mounting angle and shelves hiding feet add error that grows with distance. Accuracy is therefore stated by zone, not by centimetre."],
      ["A zone state machine records entering, leaving and stopping: staying within about one metre for five seconds or more. Presence time and engaged time are kept separate.",
        "Thresholds are configurable, because a furniture store behaves differently from a grocery."],
      ["Only track number, time, x and y are written, in small batches. Heat maps, queue estimates, flags and before and after comparisons are computed from those rows.",
        "Raw coordinates are tiny, so a layout can be compared week over week without keeping any video."],
    ];
    const box = $("#stage-detail"), btns = $$("#stages button");
    if (!box) return;
    const show = (i) => {
      btns.forEach((b, k) => b.setAttribute("aria-selected", String(k === i)));
      box.innerHTML = "";
      const p = document.createElement("p"); p.textContent = STAGES[i][0];
      const f = document.createElement("p"); f.className = "frag";
      const b = document.createElement("b"); b.textContent = "Where it is fragile: ";
      f.appendChild(b); f.appendChild(document.createTextNode(STAGES[i][1]));
      box.append(p, f);
    };
    btns.forEach((b, i) => { b.addEventListener("click", () => show(i)); b.addEventListener("mouseenter", () => show(i)); });
    show(0);
  }

  function initQuad() {
    const qs = $$("#quad .q");
    qs.forEach((q) => q.addEventListener("click", () => qs.forEach((x) => x.classList.toggle("on", x === q && !q.classList.contains("on")))));
  }

  function initMission() {
    const cards = $$("#mission .mcard"), note = $("#mission-note");
    const T = {
      directed: "Directed shoppers have short dwell and a high chance of buying. Reading their short stop as the display being ignored would be wrong: they knew what they wanted and took it.",
      exploratory: "Explorers dwell longer, and whether they buy is less certain. A zone that converts directed shoppers but not explorers needs a different fix, such as clearer pricing or a way to compare.",
      loiterer: "Someone sheltering from the heat or waiting for a friend should not inflate a display's dwell. They are counted as a segment of their own, with a confidence score, and a spike at a certain time of day is itself a finding.",
    };
    const set = (k) => { cards.forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.m === k))); note.textContent = T[k]; };
    cards.forEach((c) => { c.addEventListener("click", () => set(c.dataset.m)); c.addEventListener("mouseenter", () => set(c.dataset.m)); });
    set("directed");
  }

  function initQueue() {
    const l = $("#c-l"), r = $("#c-r");
    if (!l) return;
    const bar = $("#c-bar"), verdict = $("#c-verdict");
    const upd = () => {
      const L = +l.value, R = +r.value, wait = L / R;
      $("#c-lo").textContent = L; $("#c-ro").textContent = R.toFixed(1);
      bar.style.width = Math.min(100, (wait / 10) * 100) + "%";
      bar.style.background = wait >= 4 ? "var(--bad)" : wait >= 3 ? "var(--warn)" : "var(--good)";
      verdict.innerHTML = "";
      const em = document.createElement("em"); em.textContent = wait.toFixed(1) + " min estimated wait";
      verdict.append(em, document.createTextNode(wait >= 4
        ? "That is a bottleneck. Open another till, or send someone to the front."
        : wait >= 3 ? "Getting close to the 4 minute line. Watch it." : "A comfortable queue."));
    };
    l.addEventListener("input", upd); r.addEventListener("input", upd); upd();
  }

  function initNorm() {
    const t = $("#n-t"), s = $("#n-s");
    if (!t) return;
    const A = { n: 200, stops: 60 }, shareA = A.stops / A.n;
    const sgn = (v) => (v >= 0 ? "+" : "−") + Math.abs(Math.round(v * 100)) + "%";
    const upd = () => {
      const T = +t.value; s.max = T; if (+s.value > T) s.value = T;
      const S = +s.value, shareB = S / T, raw = S / A.stops - 1, real = shareB / shareA - 1;
      $("#n-to").textContent = T; $("#n-so").textContent = S;
      $("#n-b").textContent = T + " shoppers, " + S + " stopped";
      $("#n-bs").textContent = Math.round(shareB * 100) + "%";
      const rawEl = $("#n-raw"), shEl = $("#n-share");
      rawEl.textContent = sgn(raw); shEl.textContent = sgn(real);
      [rawEl, shEl].forEach((e, i) => { const v = i ? real : raw; e.className = Math.abs(v) < 0.05 ? "" : v > 0 ? "pos" : "neg"; });
      $("#n-verdict").textContent = Math.abs(real) < 0.1 && Math.abs(raw) >= 0.1
        ? "A busier day made the raw count look like a win. As a share of shoppers, nothing really changed."
        : Math.abs(real) < 0.1 ? "No meaningful change in the share of shoppers who stopped."
        : real > 0 ? "Even after allowing for traffic, a larger share of shoppers stopped. In the app this comes with a likely range."
        : "Even after allowing for traffic, a smaller share of shoppers stopped.";
    };
    t.addEventListener("input", upd); s.addEventListener("input", upd); upd();
  }

  document.addEventListener("DOMContentLoaded", () => {
    initHero(); initDeck(); initFork(); initWalker(); initTable(); initShred(); initFeed(); initStages();
    initQuad(); initMission(); initQueue(); initNorm();
  });
})();
