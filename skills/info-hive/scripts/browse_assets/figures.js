/* 閲覧（browse）の図の描画（ADR-0030、図の描画の設計の 7.4）。
 *
 * 本文の ```mermaid のフェンス（<pre><code class="language-mermaid">）を、図として描く。
 * - サーバーの方式（http と https）でだけ動く。静的の書き出し（file://）では何もしない。ブラウザは、sandbox の枠に
 *   隣のファイルのスクリプトを読ませない（PoC）。
 * - 図 1 つにつき、見えない sandbox の枠（トークンのない /_frame/<版>/ の文書）を 1 つ作り、その図のソースと配色だけを
 *   送る。枠が返す SVG の文字列は DOM に入れず、data: の <img> にして出す。描き終えたら枠を消す。
 * - 描画は「回」として持つ。新しい回（配色が変わったとき）を始めるときは、前の回の枠をすべて消す。同時に生きている
 *   枠は、どの時点でも 2 つまでである。
 * - 枠とのやり取りは使い切りである。ready を 1 回受けて送り、result を 1 回受けたら、すぐに枠を消して受け取りをやめる。
 * - 触るのは、自分で作った要素（入れ物、画像、Accordion、警告、枠）の属性と、<pre> の置き場所だけである。要素の
 *   style には触らない（el.style.x = ... も、style 属性を作るので使わない）。
 * - 見た目は fluent2.css の部品（Card、Image、Accordion、MessageBar）とトークンで組む。
 */
(function () {
  "use strict";
  if (location.protocol !== "http:" && location.protocol !== "https:") return;
  var body = document.getElementById("_body");
  if (!body) return;
  var codes = body.querySelectorAll("pre > code.language-mermaid");
  if (!codes.length) return;

  // 中核の FRAME_VERSION と同じ値（試験が一致を確かめる）
  var FRAME_VERSION = "mermaid-12.3.0-1";
  var FRAME_URL = location.origin + "/_frame/" + FRAME_VERSION + "/mermaid-frame.html";
  var CONCURRENCY = 2;
  var TIMEOUT_MS = 20000;
  var MAX_SVG = 5000000;
  var MAX_SIDE = 20000;
  var NS = "http://www.w3.org/2000/svg";
  // MessageBar の警告のアイコン（Fluent UI System Icons の WarningFilled、20px。MIT。中核の ICON_WARNING と同じ形）
  var WARNING_PATH = "M7.37 3.56a3 3 0 0 1 5.26 0l5.5 10A3 3 0 0 1 15.5 18h-11a3 3 0 0 1-2.63-4.44z" +
    "M10 12.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5m0-6.25a.5.5 0 0 0-.5.5v4a.5.5 0 0 0 1 0V7a.5.5 0 0 0-.5-.5";
  // Accordion の山形のアイコン（Fluent UI System Icons の ChevronRightRegular、20px。MIT）
  var CHEVRON_PATH = "M7.65 4.15c.2-.2.5-.2.7 0l5.49 5.46c.21.22.21.57 0 .78l-5.49 5.47a.5.5 0 0 1-.7-.71L12.8 10 7.65 4.85a.5.5 0 0 1 0-.7";

  // Fluent のトークン（fluent2.css の CSS 変数）から、Mermaid の base テーマの変数を作る（図の描画の設計の 8）
  var TOKENS = {
    background: "--colorNeutralBackground1",
    edgeLabelBackground: "--colorNeutralBackground1",
    primaryColor: "--colorBrandBackground2",
    mainBkg: "--colorBrandBackground2",
    primaryBorderColor: "--colorBrandStroke1",
    nodeBorder: "--colorBrandStroke1",
    primaryTextColor: "--colorNeutralForeground1",
    secondaryTextColor: "--colorNeutralForeground1",
    tertiaryTextColor: "--colorNeutralForeground1",
    textColor: "--colorNeutralForeground1",
    titleColor: "--colorNeutralForeground1",
    noteTextColor: "--colorNeutralForeground1",
    secondaryColor: "--colorNeutralBackground3",
    secondaryBorderColor: "--colorNeutralStroke1",
    noteBorderColor: "--colorNeutralStroke1",
    tertiaryColor: "--colorNeutralBackground2",
    clusterBkg: "--colorNeutralBackground2",
    tertiaryBorderColor: "--colorNeutralStroke2",
    clusterBorder: "--colorNeutralStroke2",
    lineColor: "--colorNeutralForeground2",
    noteBkgColor: "--colorPaletteYellowBackground1"
  };

  function hex(value) {
    var v = (value || "").trim();
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
    if (m) {
      var h = m[1];
      return "#" + (h.length === 3 ? h.replace(/./g, "$&$&") : h).toLowerCase();
    }
    m = /^rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/i.exec(v);
    if (m) {
      return "#" + [m[1], m[2], m[3]].map(function (n) {
        var s = Math.max(0, Math.min(255, +n)).toString(16);
        return s.length < 2 ? "0" + s : s;
      }).join("");
    }
    return null;
  }

  function luminance(h) {
    var n = parseInt(h.slice(1), 16);
    return (0.2126 * (n >> 16 & 255) + 0.7152 * (n >> 8 & 255) + 0.0722 * (n & 255)) / 255;
  }

  function readTheme() {
    var cs = getComputedStyle(document.documentElement);
    var vars = {};
    Object.keys(TOKENS).forEach(function (k) {
      var h = hex(cs.getPropertyValue(TOKENS[k]));
      if (h) vars[k] = h;
    });
    vars.fontFamily = cs.getPropertyValue("--fontFamilyBase").trim() || "sans-serif";
    vars.fontSize = cs.getPropertyValue("--fontSizeBase300").trim() || "14px";
    vars.darkMode = vars.background ? luminance(vars.background) < 0.5 : false;
    return { vars: vars, key: JSON.stringify(vars) };
  }

  function icon(d) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "fui-Icon");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
    return svg;
  }

  var items = [];
  for (var i = 0; i < codes.length; i++) {
    var pre = codes[i].parentNode;
    items.push({ index: i, pre: pre, parent: pre.parentNode, next: pre.nextSibling, source: codes[i].textContent,
                 view: "code", figure: null, img: null, details: null, panel: null, bar: null });
  }

  // 図の表示を「コード」「画像」「警告」のどれか 1 つにする（図の描画の設計の 7.4 の 7）
  function restoreCode(it) {
    if (it.figure) { it.figure.remove(); it.figure = null; it.img = null; }
    if (it.details) { it.details.remove(); it.details = null; it.panel = null; }
    if (it.bar) { it.bar.remove(); it.bar = null; }
    if (it.pre.parentNode !== it.parent) it.parent.insertBefore(it.pre, it.next && it.next.parentNode === it.parent ? it.next : null);
    it.view = "code";
  }

  function showImage(it, url, msg) {
    if (it.view !== "image") {
      restoreCode(it);
      it.figure = document.createElement("figure");
      it.figure.className = "wiki-fig fui-Card";
      it.figure.setAttribute("data-fig", "mermaid");
      var img = document.createElement("img");
      img.className = "fui-Image";
      // 画像として読めない SVG（XML として壊れたものなど）は、警告に替える
      img.addEventListener("error", function () {
        if (it.img === img && it.view === "image") showWarning(it);
      });
      it.img = img;
      it.figure.appendChild(img);
      it.details = document.createElement("details");
      it.details.className = "fui-AccordionItem wiki-fig-source";
      var summary = document.createElement("summary");
      summary.className = "fui-AccordionHeader__button";
      var chevron = document.createElement("span");
      chevron.className = "fui-AccordionHeader__expandIcon";
      chevron.setAttribute("aria-hidden", "true");
      chevron.appendChild(icon(CHEVRON_PATH));
      summary.appendChild(chevron);
      summary.appendChild(document.createTextNode("図のコード"));
      it.panel = document.createElement("div");
      it.panel.className = "fui-AccordionPanel";
      it.details.appendChild(summary);
      it.details.appendChild(it.panel);
      it.parent.insertBefore(it.figure, it.pre);
      it.parent.insertBefore(it.details, it.pre);
      it.panel.appendChild(it.pre);
      it.view = "image";
    }
    it.img.setAttribute("alt", typeof msg.title === "string" && msg.title ? msg.title.slice(0, 200) : "図");
    it.img.setAttribute("width", String(Math.max(1, Math.round(msg.w))));
    it.img.setAttribute("height", String(Math.max(1, Math.round(msg.h))));
    it.img.setAttribute("src", url);
  }

  var barSeq = 0;
  function showWarning(it) {
    if (it.view === "warning") return;
    restoreCode(it);
    barSeq += 1;
    var id = "_figwarn" + barSeq;
    it.bar = document.createElement("div");
    it.bar.className = "wiki-fig-error fui-MessageBar";
    it.bar.setAttribute("role", "group");
    it.bar.setAttribute("aria-labelledby", id);
    it.bar.setAttribute("data-intent", "warning");
    it.bar.setAttribute("data-layout", "singleline");
    var ic = document.createElement("div");
    ic.className = "fui-MessageBar__icon";
    ic.setAttribute("aria-hidden", "true");
    ic.appendChild(icon(WARNING_PATH));
    var text = document.createElement("div");
    text.className = "fui-MessageBarBody";
    text.id = id;
    text.textContent = "図を描けなかった。コードを表示している。";
    it.bar.appendChild(ic);
    it.bar.appendChild(text);
    it.parent.insertBefore(it.bar, it.pre);
    it.view = "warning";
  }

  // 枠の結果を確かめ、画像の URL を作る。使えなければ null
  function imageUrl(d) {
    if (d.ok !== true || typeof d.svg !== "string" || d.svg.length > MAX_SVG || d.svg.slice(0, 4) !== "<svg") return null;
    if (typeof d.w !== "number" || typeof d.h !== "number" || !(d.w > 0 && d.h > 0 && d.w <= MAX_SIDE && d.h <= MAX_SIDE)) return null;
    try {
      return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(d.svg);
    } catch (e) {
      return null;     // 対になっていないサロゲートなど
    }
  }

  // 1 つの回。stop() で、生きている枠をすべて消し、受け取りをやめる
  function Run(theme) {
    this.theme = theme;
    this.queue = items.slice();
    this.live = [];
    this.stopped = false;
  }

  Run.prototype.stop = function () {
    this.stopped = true;
    this.queue = [];
    this.live.slice().forEach(function (f) { f.finish(null); });
  };

  Run.prototype.start = function () {
    for (var n = 0; n < CONCURRENCY; n++) this.next();
  };

  Run.prototype.next = function () {
    if (this.stopped) return;
    var it = this.queue.shift();
    if (!it) return;
    this.open(it);
  };

  // 図 1 つの枠。状態は "wait-ready"、"wait-result"、"done" の 3 つ
  Run.prototype.open = function (it) {
    var run = this;
    var frame = document.createElement("iframe");
    var state = "wait-ready";
    var timer = null;
    var handle = { finish: finish };

    function finish(result) {
      if (state === "done") return;
      state = "done";
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      frame.remove();
      run.live.splice(run.live.indexOf(handle), 1);
      if (run.stopped) return;
      var url = result ? imageUrl(result) : null;
      if (url) showImage(it, url, result); else showWarning(it);
      run.next();
    }

    function onMessage(e) {
      if (e.source !== frame.contentWindow || state === "done") return;
      var d = e.data;
      if (!d || typeof d !== "object") return;
      if (state === "wait-ready" && d.type === "ready") {
        state = "wait-result";
        frame.contentWindow.postMessage({ type: "render", source: it.source, theme: run.theme.vars }, "*");
      } else if (state === "wait-result" && d.type === "result") {
        finish(d);
      }
    }

    frame.className = "fig-frame";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("title", "図の描画");
    frame.setAttribute("aria-hidden", "true");
    frame.setAttribute("tabindex", "-1");
    frame.setAttribute("width", "1200");
    frame.setAttribute("height", "800");
    window.addEventListener("message", onMessage);
    run.live.push(handle);
    timer = setTimeout(function () { finish(null); }, TIMEOUT_MS);
    frame.src = FRAME_URL;
    document.body.appendChild(frame);
  };

  var current = null;
  function render() {
    var theme = readTheme();
    if (current && current.theme.key === theme.key) return;
    if (current) current.stop();
    current = new Run(theme);
    current.start();
  }

  new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", render);
  }
  render();
})();
