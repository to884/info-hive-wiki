/* wiki の閲覧（browse）の画面。classic なスクリプトだけを使い、fetch、ES モジュール、Web Worker を使わない。
   ページの HTML には本文、メタ欄、被リンク、関係表、診断が描いてあるので、このスクリプトはナビゲーションを
   組み立て、データの世代を確かめるだけである。インラインの style は使わず、クラスだけを付ける。 */
(function () {
  "use strict";
  var doc = document.documentElement;
  var root = doc.getAttribute("data-root") || "";
  var generation = doc.getAttribute("data-generation") || "";
  var state = window.__browse = { navLoaded: false, errors: [], stale: false, mismatch: [] };

  function href(path) {
    return root + path.split("/").map(encodeURIComponent).join("/");
  }

  function notice(text, cls) {
    var main = document.getElementById("_main");
    if (!main) { return; }
    var p = document.createElement("p");
    p.className = "notice " + cls;
    p.textContent = text;
    main.insertBefore(p, main.firstChild);
  }

  var data = [["WIKI_NAV", window.WIKI_NAV], ["WIKI_MANIFEST", window.WIKI_MANIFEST],
              ["WIKI_DIAGNOSTICS", window.WIKI_DIAGNOSTICS]];
  for (var i = 0; i < data.length; i++) {
    var value = data[i][1];
    if (!value) { continue; }
    if (value.protocol_version !== 1) {
      state.errors.push("未対応の契約の版: " + data[i][0]);
      notice("この画面は、データの契約の版に対応していない。", "notice-error");
      return;
    }
    if (value.generation !== generation) {
      state.mismatch.push(data[i][0]);
    }
  }
  if (state.mismatch.length) {
    notice("Wiki が更新された。再読み込みする。", "notice-updated");
  }
  if (window.WIKI_MANIFEST && window.WIKI_MANIFEST.stale) {
    state.stale = true;
  }

  var nav = document.getElementById("_nav");
  if (!nav || !window.WIKI_NAV) {
    state.errors.push("nav.js を読めない");
    return;
  }

  function item(it) {
    var li = document.createElement("li");
    if (it.state === "resolved") {
      var a = document.createElement("a");
      a.href = href(it.path) + (it.anchor ? "#" + encodeURIComponent(it.anchor) : "");
      a.textContent = it.label;
      li.appendChild(a);
    } else if (it.state === "group") {
      var g = document.createElement("a");
      g.href = href("index.html") + "#" + encodeURIComponent(it.group);
      g.textContent = it.label;
      li.appendChild(g);
    } else {
      var span = document.createElement("span");
      span.className = "wl wl-" + it.state;
      span.textContent = it.label;
      li.appendChild(span);
    }
    return li;
  }

  function group(g, depth) {
    var section = document.createElement("section");
    section.className = "nav-group nav-depth-" + Math.min(depth, 6);
    if (g.title) {
      var h = document.createElement("h2");
      h.textContent = g.title;
      section.appendChild(h);
    }
    if (g.items.length) {
      var ul = document.createElement("ul");
      for (var k = 0; k < g.items.length; k++) { ul.appendChild(item(g.items[k])); }
      section.appendChild(ul);
    }
    for (var c = 0; c < g.children.length; c++) { section.appendChild(group(g.children[c], depth + 1)); }
    return section;
  }

  var home = document.createElement("a");
  home.href = href("index.html");
  home.className = "nav-home";
  home.textContent = "索引";
  nav.appendChild(home);
  var groups = window.WIKI_NAV.groups || [];
  for (var n = 0; n < groups.length; n++) { nav.appendChild(group(groups[n], 0)); }
  state.navLoaded = true;
})();
