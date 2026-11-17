/* 閲覧（browse）の図の描画の枠の側（ADR-0030、図の描画の設計の 7.5）。
 *
 * Mermaid（@mermaid-js/tiny 12.3.0、MIT ライセンス、Copyright (c) 2014 - 2022 Knut Sveidqvist）を、sandbox の枠
 * （不透明なオリジン）の中で動かす。束の中の第三者の表示は browse_assets/licenses/ にある。
 *
 * - 親（window.parent）からの最初の render だけを受け、1 つの図を描いて、result を 1 回返す。読み込みのあとに ready を
 *   1 回返す。親の DOM には触れない。
 * - securityLevel は strict、htmlLabels は使わない。図の側（%%{init}%% など）から、見た目と安全に関わる設定を変え
 *   させない（secure）。
 * - result には、枠の中で数えた CSP の違反を添える（試験が使う。親は画面に出さない）。
 */
(function () {
  "use strict";
  var violations = [];
  document.addEventListener("securitypolicyviolation", function (e) {
    violations.push(String(e.violatedDirective) + " " + String(e.blockedURI || ""));
  });
  var parentWin = window.parent;
  var used = false;

  function post(message) { parentWin.postMessage(message, "*"); }

  // 図の側から変えられない設定。Mermaid の既定の secure に足す
  var SECURE = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "suppressErrorRendering", "maxEdges",
                "theme", "themeVariables", "themeCSS", "fontFamily", "htmlLabels", "darkMode", "look"];

  function config(vars) {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      theme: "base",
      themeVariables: vars,
      fontFamily: vars.fontFamily,
      secure: SECURE
    };
  }

  // 描いた SVG を、画像として使える文字にする。大きさは viewBox から決め、根の style を外す
  function normalize(text) {
    var host = document.createElement("div");
    document.body.appendChild(host);
    try {
      host.innerHTML = text;
      var svg = host.querySelector("svg");
      if (!svg) throw new Error("svg がない");
      var vb = (svg.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
      var box = vb.length === 4 && vb[2] > 0 && vb[3] > 0 ? { width: vb[2], height: vb[3] } : svg.getBBox();
      svg.setAttribute("width", String(box.width));
      svg.setAttribute("height", String(box.height));
      svg.removeAttribute("style");
      var title = svg.querySelector("title");
      return { svg: new XMLSerializer().serializeToString(svg), w: box.width, h: box.height,
               title: title ? title.textContent : null };
    } finally {
      host.remove();
    }
  }

  function finish(message) {
    // 違反のイベントは非同期に届くので、少し待ってから数える
    setTimeout(function () {
      message.type = "result";
      message.violations = violations.slice(0, 20);
      post(message);
    }, 100);
  }

  window.addEventListener("message", function (e) {
    if (e.source !== parentWin || used) return;
    var d = e.data;
    if (!d || d.type !== "render" || typeof d.source !== "string") return;
    used = true;
    try {
      mermaid.initialize(config(d.theme && typeof d.theme === "object" ? d.theme : {}));
    } catch (err) {
      finish({ ok: false, error: String(err && err.message || err).slice(0, 300) });
      return;
    }
    mermaid.render("fig", d.source).then(function (r) {
      var n = normalize(r.svg);
      finish({ ok: true, svg: n.svg, w: n.w, h: n.h, title: n.title });
    }, function (err) {
      finish({ ok: false, error: String(err && err.message || err).slice(0, 300) });
    }).catch(function (err) {
      finish({ ok: false, error: String(err && err.message || err).slice(0, 300) });
    });
  });

  post({ type: typeof mermaid === "undefined" ? "load-error" : "ready" });
})();
