/* Shared helpers: theme, heat-map colours and intensity, Plotly layout. Everything redraws on change. */
(function () {
  const root = document.documentElement;
  const renderers = [];

  /* Heat palettes. Every ramp starts with a clearly coloured floor tone, so low-traffic areas are visible
     rather than fading into the page. Each has a light-page and a dark-page version. */
  const PALETTES = {
    thermal: { name: "Thermal",
      light: ["#5B6FD6", "#7E5BC2", "#C2459B", "#E8603C", "#F7A21B", "#FFE45C"],
      dark: ["#2F3F9C", "#5A3FA3", "#B23A8F", "#EE6A3A", "#FFB02E", "#FFF07A"] },
    viridis: { name: "Viridis",
      light: ["#472D7B", "#3B528B", "#21908C", "#27AD81", "#85D54A", "#FDE725"],
      dark: ["#4B3A93", "#3F5F9E", "#21908C", "#27AD81", "#85D54A", "#FDE725"] },
    magma: { name: "Magma",
      light: ["#51127C", "#8C2981", "#D3436E", "#F8765C", "#FEBA80", "#FCFDBF"],
      dark: ["#6A2796", "#A0399A", "#E04F83", "#FA8466", "#FEC48C", "#FCFDBF"] },
    ocean: { name: "Ocean",
      light: ["#1F4E9E", "#2A7AB8", "#2FA4B5", "#5CC8A8", "#B5E48C", "#F9F871"],
      dark: ["#2D5FB8", "#3B8AC8", "#3FB4C4", "#6FD6B4", "#BFEA94", "#F9F871"] },
    cividis: { name: "Colour-blind safe",
      light: ["#00306F", "#33497A", "#68696F", "#99896F", "#CDB964", "#FFE945"],
      dark: ["#2B5BA8", "#4E6E9C", "#7F8186", "#A99B7A", "#D6C46A", "#FFEA4A"] },
    mono: { name: "Single hue",
      light: ["#9DB2EC", "#7A97E3", "#5577D6", "#2F55C5", "#1E3FA0", "#0F2468"],
      dark: ["#2F4688", "#456BC4", "#6C93F0", "#9DB8FF", "#CCDBFF", "#FFFFFF"] },
  };
  const DEFAULTS = { palette: "thermal", intensity: 65 };

  const theme = () => (root.dataset.theme === "dark" ? "dark" : "light");
  const css = (n) => getComputedStyle(root).getPropertyValue(n).trim();

  const heat = Object.assign({}, DEFAULTS);
  try {
    const saved = JSON.parse(localStorage.getItem("heat") || "{}");
    if (PALETTES[saved.palette]) heat.palette = saved.palette;
    if (Number.isFinite(saved.intensity)) heat.intensity = Math.max(0, Math.min(100, saved.intensity));
  } catch (e) { /* storage blocked: defaults */ }

  /* Intensity 50 shows values as measured. Higher lifts quiet areas, lower keeps only the peaks. */
  const gamma = () => Math.pow(2, (50 - heat.intensity) / 25);
  const stops = () => PALETTES[heat.palette][theme()].map((c, i, a) => [i / (a.length - 1), c]);

  function setTheme(t) {
    root.dataset.theme = t;
    try { localStorage.setItem("theme", t); } catch (e) { /* storage blocked */ }
    const b = document.getElementById("theme-toggle");
    if (b) { b.textContent = t === "dark" ? "Light theme" : "Dark theme"; b.setAttribute("aria-pressed", String(t === "dark")); }
    syncControls();
    renderAll();
    document.dispatchEvent(new CustomEvent("heatchange"));
  }

  function setHeat(part) {
    Object.assign(heat, part);
    try { localStorage.setItem("heat", JSON.stringify(heat)); } catch (e) { /* storage blocked */ }
    syncControls();
    renderAll();
    document.dispatchEvent(new CustomEvent("heatchange"));
  }

  function renderAll() { renderers.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }); }

  function rampCss() {
    return "linear-gradient(90deg," + stops().map((s) => s[1] + " " + (s[0] * 100) + "%").join(",") + ")";
  }

  /* ---- heat controls (intensity slider and palette picker), shared by every page that has them */
  function buildControls() {
    document.querySelectorAll("[data-heat-palette]").forEach((box) => {
      if (box.dataset.built) return;
      box.dataset.built = "1";
      Object.entries(PALETTES).forEach(([key, p]) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "swatch"; b.dataset.key = key; b.title = p.name;
        b.setAttribute("aria-label", p.name + " colours");
        b.innerHTML = '<span class="chip"></span><span class="nm">' + p.name + "</span>";
        b.addEventListener("click", () => setHeat({ palette: key }));
        box.appendChild(b);
      });
    });
    document.querySelectorAll("[data-heat-intensity]").forEach((el) => {
      if (el.dataset.bound) return;
      el.dataset.bound = "1";
      el.addEventListener("input", () => setHeat({ intensity: +el.value }));
    });
    document.querySelectorAll("[data-heat-reset]").forEach((el) => {
      if (el.dataset.bound) return;
      el.dataset.bound = "1";
      el.addEventListener("click", () => setHeat(Object.assign({}, DEFAULTS)));
    });
  }

  function syncControls() {
    document.querySelectorAll("[data-heat-palette] .swatch").forEach((b) => {
      const p = PALETTES[b.dataset.key][theme()];
      b.querySelector(".chip").style.background = "linear-gradient(90deg," + p.join(",") + ")";
      b.setAttribute("aria-pressed", String(b.dataset.key === heat.palette));
    });
    document.querySelectorAll("[data-heat-intensity]").forEach((el) => { el.value = heat.intensity; });
    document.querySelectorAll("[data-heat-readout]").forEach((el) => {
      el.textContent = heat.intensity === 50 ? "as measured" : heat.intensity > 50 ? "quiet areas lifted" : "peaks only";
    });
    document.querySelectorAll("[data-ramp]").forEach((el) => { el.style.background = rampCss(); });
  }

  function ready() {
    const b = document.getElementById("theme-toggle");
    if (b) {
      b.textContent = theme() === "dark" ? "Light theme" : "Dark theme";
      b.setAttribute("aria-pressed", String(theme() === "dark"));
      b.addEventListener("click", () => setTheme(theme() === "dark" ? "light" : "dark"));
    }
    buildControls();
    syncControls();
    if (!window.Plotly) {
      document.querySelectorAll("[data-plot]").forEach((el) => {
        el.innerHTML = '<p class="empty">Charts need Plotly.js, which loads from a CDN. Check the internet connection and reload.</p>';
      });
      return;
    }
    renderAll();
  }

  function baseLayout(extra) {
    return Object.assign({
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: css("--body"), color: css("--ink-2"), size: 13 },
      margin: { l: 44, r: 12, t: 8, b: 40 },
    }, extra || {});
  }

  /* Store-plan layout: fixed metres, 1:1 aspect, zones drawn as outlined floor areas. */
  function mapLayout(p, extra) {
    const line = css("--zone-line");
    const shapes = [], notes = [];
    Object.entries(p.zones).forEach(([name, z]) => {
      shapes.push({ type: "rect", x0: z.x0, y0: z.y0, x1: z.x1, y1: z.y1, line: { color: line, width: 2 }, fillcolor: "rgba(0,0,0,0)", layer: "above" });
      notes.push({ x: (z.x0 + z.x1) / 2, y: z.y1 - 0.15, text: name, showarrow: false, yanchor: "top", font: { size: 12, color: "#fff" },
        bgcolor: "rgba(10,18,30,0.62)", borderpad: 2 });
    });
    return baseLayout(Object.assign({
      shapes, annotations: notes,
      xaxis: { range: [0, p.store.w], constrain: "domain", ticksuffix: " m", showgrid: false, zeroline: false, fixedrange: true, tickfont: { size: 11 } },
      yaxis: { range: [0, p.store.h], scaleanchor: "x", ticksuffix: " m", showgrid: false, zeroline: false, fixedrange: true, tickfont: { size: 11 } },
      margin: { l: 44, r: 12, t: 6, b: 32 },
      hoverlabel: { font: { family: css("--body") } },
    }, extra || {}));
  }

  /* Heat trace. Values are scaled by the shared maximum and raised to the intensity exponent, so the
     colour bar and tooltips still show the real seconds. */
  function heatTrace(p, opts) {
    opts = opts || {};
    const zmax = opts.zmax || Math.max(1e-9, ...p.z.flat());
    const g = gamma();
    const z = p.z.map((row) => row.map((v) => Math.pow(Math.max(v, 0) / zmax, g)));
    const fr = [0, 0.25, 0.5, 0.75, 1];
    return {
      type: "heatmap", z: z, x: p.x, y: p.y, customdata: p.z, colorscale: stops(), zmin: 0, zmax: 1,
      zsmooth: "best", showscale: !!opts.showscale,
      colorbar: {
        title: { text: p.unit, side: "right" }, thickness: 10, len: 0.85, outlinewidth: 0, tickfont: { size: 11 },
        tickmode: "array", tickvals: fr.map((f) => Math.pow(f, g)), ticktext: fr.map((f) => (f * zmax).toFixed(zmax >= 10 ? 0 : 1)),
      },
      hovertemplate: "%{customdata:.1f} " + p.unit + "<br>x %{x:.1f} m, y %{y:.1f} m<extra></extra>",
      hoverinfo: opts.nohover ? "skip" : undefined,
    };
  }

  function plot(el, data, layout, config) {
    return Plotly.react(el, data, layout, Object.assign({ displayModeBar: false, responsive: true }, config || {}));
  }

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " returned " + r.status);
    return r.json();
  }

  window.App = {
    theme, css, PALETTES, stops, gamma, heat, setHeat, baseLayout, mapLayout, heatTrace, rampCss, plot, getJSON,
    onRender(fn) { renderers.push(fn); },
    run: ready,
  };
  document.addEventListener("DOMContentLoaded", () => setTimeout(() => App.run(), 0));
})();

// Copy buttons for command blocks and a freshly created API key.
(function () {
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy") ? resolve() : reject(); } catch (e) { reject(e); } finally { ta.remove(); }
    });
  }
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("pre.code, #newkey").forEach((el) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn copy"; b.textContent = "Copy";
      b.setAttribute("aria-label", el.id === "newkey" ? "Copy the new API key" : "Copy this command");
      b.addEventListener("click", () => {
        copyText(el.textContent.trim()).then(
          () => { b.textContent = "Copied"; },
          () => { b.textContent = "Select and copy by hand"; });
        setTimeout(() => { b.textContent = "Copy"; }, 2000);
      });
      el.insertAdjacentElement("afterend", b);
    });
  });
})();
