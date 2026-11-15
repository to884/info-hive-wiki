/* wiki の閲覧（browse）の画面。classic なスクリプトだけを使い、fetch、ES モジュール、Web Worker を使わない。
   ページの HTML には本文、メタ欄、被リンク、関係表、診断、索引の一覧が描いてあるので、このスクリプトは
   ナビゲーションの木、検索、索引の一覧の絞り込み、配色の切り替えを組み立て、データの世代を確かめるだけである。
   要素を作るほかに変えるのは、class、hidden、open、aria-* と、<html> の data-theme（theme.js を通す）だけで、
   要素の style には触らない。部品のマークアップは Fluent 2 の設計（docs/plans/wiki-browse-fluent2-2026-11-14.md）の
   4 節、ふるまいは探しやすさの設計（docs/plans/wiki-browse-findability-2026-11-13.md）の 3〜5 節による。 */
(function () {
  "use strict";
  var PROTOCOL = 2;
  var doc = document.documentElement;
  var root = doc.getAttribute("data-root") || "";
  var generation = doc.getAttribute("data-generation") || "";
  var state = window.__browse = { navLoaded: false, errors: [], stale: false, mismatch: [], searchLoaded: false,
                                  search: function () { return null; } };
  var TYPE_LABELS = { source: "ソース", entity: "エンティティ", concept: "コンセプト", synthesis: "シンセシス" };
  var GROUP_ID = /^_g[0-9a-z]+$/;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var ICONS = {
    chevron: "M15.85 7.65c.2.2.2.5 0 .7l-5.46 5.49a.55.55 0 0 1-.78 0L4.15 8.35a.5.5 0 1 1 .7-.7L10 12.8l5.15-5.16c.2-.2.5-.2.7 0",
    search: "M13.73 14.44a6.5 6.5 0 1 1 .7-.7l3.42 3.4a.5.5 0 0 1-.63.77l-.07-.06zm-.71-.71A5.5 5.5 0 0 0 15 9.5a5.5 5.5 0 1 0-1.98 4.23",
    dismiss: "m4.09 4.22.06-.07a.5.5 0 0 1 .63-.06l.07.06L10 9.29l5.15-5.14a.5.5 0 0 1 .63-.06l.07.06c.18.17.2.44.06.63l-.06.07L10.71 10l5.14 5.15c.18.17.2.44.06.63l-.06.07a.5.5 0 0 1-.63.06l-.07-.06L10 10.71l-5.15 5.14a.5.5 0 0 1-.63.06l-.07-.06a.5.5 0 0 1-.06-.63l.06-.07L9.29 10 4.15 4.85a.5.5 0 0 1-.06-.63l.06-.07z",
    warning: "M7.37 3.56a3 3 0 0 1 5.26 0l5.5 10A3 3 0 0 1 15.5 18h-11a3 3 0 0 1-2.63-4.44zM10 12.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5m0-6.25a.5.5 0 0 0-.5.5v4a.5.5 0 0 0 1 0V7a.5.5 0 0 0-.5-.5",
    error: "M7.88 1.88a3 3 0 0 1 4.24 0l6 6a3 3 0 0 1 0 4.24l-6 6a3 3 0 0 1-4.24 0l-6-6a3 3 0 0 1 0-4.24zM13.1 6.9a.5.5 0 0 0-.7 0L10 9.3 7.6 6.9a.5.5 0 0 0-.7.7L9.3 10l-2.4 2.4a.5.5 0 0 0 .7.7l2.4-2.4 2.4 2.4a.5.5 0 0 0 .7-.7L10.7 10l2.4-2.4a.5.5 0 0 0 0-.7"
  };

  function has(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function typeLabel(t) { return has(TYPE_LABELS, t) ? TYPE_LABELS[t] : (t || "種類なし"); }
  function typeClass(t) { return /^[a-z][a-z0-9_-]*$/.test(t || "") ? "t-" + t : "t-other"; }
  function href(path) { return root + String(path).split("/").map(encodeURIComponent).join("/"); }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }

  function icon(name, size) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "fui-Icon");
    svg.setAttribute("width", size || "20");
    svg.setAttribute("height", size || "20");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", ICONS[name]);
    svg.appendChild(p);
    return svg;
  }

  // ページの先頭の知らせ（Fluent の MessageBar）。cls は notice-updated、notice-error など、試験が使う印
  var noticeCount = 0;
  function notice(title, body, cls, intent) {
    var main = document.getElementById("_main");
    if (!main) { return; }
    noticeCount += 1;
    var id = "_notice" + noticeCount;
    var bar = el("div", "notice " + cls + " fui-MessageBar");
    bar.id = id;
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-labelledby", id + "_title");
    bar.setAttribute("data-intent", intent || "warning");
    bar.setAttribute("data-layout", "multiline");
    var ic = el("div", "fui-MessageBar__icon");
    ic.setAttribute("aria-hidden", "true");
    ic.appendChild(icon(intent === "error" ? "error" : "warning"));
    var bodyEl = el("div", "fui-MessageBarBody");
    var t = el("span", "fui-MessageBarTitle", title);
    t.id = id + "_title";
    bodyEl.appendChild(t);
    if (body) { bodyEl.appendChild(document.createTextNode(body)); }
    bar.appendChild(ic);
    bar.appendChild(bodyEl);
    bar.appendChild(el("div", "fui-MessageBarActions"));
    bar.appendChild(el("div", "fui-MessageBar__bottomReflowSpacer"));
    main.insertBefore(bar, main.firstChild);
  }

  function updatedNotice() {
    if (!state.updatedShown) {
      state.updatedShown = true;
      notice("Wiki が更新された。", "再読み込みする。", "notice-updated", "warning");
    }
  }

  /* --- データの契約と世代 --- */
  var data = [["WIKI_NAV", window.WIKI_NAV], ["WIKI_MANIFEST", window.WIKI_MANIFEST],
              ["WIKI_DIAGNOSTICS", window.WIKI_DIAGNOSTICS]];
  for (var i = 0; i < data.length; i++) {
    var value = data[i][1];
    if (!value) { continue; }
    if (value.protocol_version !== PROTOCOL) {
      state.errors.push("未対応の契約の版: " + data[i][0]);
      notice("この画面は、データの契約の版に対応していない。", "", "notice-error", "error");
      return;
    }
    if (value.generation !== generation) { state.mismatch.push(data[i][0]); }
  }
  if (state.mismatch.length) { updatedNotice(); }
  var manifest = window.WIKI_MANIFEST || {};
  if (manifest.stale) { state.stale = true; }
  var incompletePages = !!(manifest.incomplete && manifest.incomplete.unreadable_pages);

  /* --- 配色の切り替え（Fluent の Select）。theme.js が描く前に当てた値を表す --- */
  function themeField() {
    var api = window.__browseTheme;
    if (!api) { return null; }
    var wrap = el("div", "theme-field");
    var label = el("label", "fui-Label theme-label", "配色");
    label.htmlFor = "_theme";
    var box = el("span", "fui-Select");
    box.setAttribute("data-size", "small");
    var select = el("select", "fui-Select__select");
    select.id = "_theme";
    var names = { auto: "OS に合わせる", light: "ライト", dark: "ダーク" };
    for (var k = 0; k < api.values.length; k++) {
      var o = el("option", "", names[api.values[k]]);
      o.value = api.values[k];
      select.appendChild(o);
    }
    select.value = api.read();
    select.addEventListener("change", function () { api.save(select.value); });
    var ic = el("span", "fui-Select__icon");
    ic.appendChild(icon("chevron", "1em"));
    box.appendChild(select);
    box.appendChild(ic);
    wrap.appendChild(label);
    wrap.appendChild(box);
    return wrap;
  }
  var head = document.querySelector("#_nav .nav-head");
  var tf = themeField();
  if (head && tf) { head.appendChild(tf); }

  var NAV = window.WIKI_NAV;
  var navBody = document.getElementById("_navbody");
  if (!navBody || !NAV) {
    state.errors.push("nav.js を読めない");
    return;
  }
  var article = document.getElementById("_article");
  var current = article ? article.getAttribute("data-page-id") : null;
  var isIndex = !article && !!document.getElementById("_index");

  /* --- 照合（5.2）。照合と強調は、同じ 1 つのそろえ方を使う --- */
  // 1 文字（符号位置）ずつ、大文字にしてから小文字にする。全体を一度に小文字にしない（語末の Σ）
  function foldChar(c) { return c.toUpperCase().toLowerCase(); }
  function fold(s) { return Array.from(String(s || "").normalize("NFC")).map(foldChar).join(""); }
  function terms(q) { return fold(q).split(/[\s　]+/).filter(function (t) { return t; }); }

  var SEARCH = null;     // 読み込んだ WIKI_SEARCH を、照合しやすい形にしたもの
  function prepare(raw) {
    var f = raw.fields;
    var at = {};
    for (var k = 0; k < f.length; k++) { at[f[k]] = k; }
    return raw.rows.map(function (r) {
      var row = { path: r[at.path], id: r[at.id], title: r[at.title], summary: r[at.summary], type: r[at.type],
                  tags: r[at.tags], headings: r[at.headings], updated: r[at.updated], group: r[at.group_path] };
      row.nTitle = fold(row.title);
      row.nSummary = fold(row.summary);
      row.nTags = row.tags.map(fold);
      row.nHeadings = row.headings.map(function (h) { return fold(h[1]); });
      return row;
    });
  }

  function score(row, ts) {
    var total = 0;
    var hit = null;
    for (var k = 0; k < ts.length; k++) {
      var t = ts[k];
      var best = 0;
      var p = row.nTitle.indexOf(t);
      if (p === 0) { best = 12; } else if (p > 0) { best = 8; }
      for (var g = 0; g < row.nTags.length; g++) {
        if (row.nTags[g] === t) { best = Math.max(best, 6); } else if (row.nTags[g].indexOf(t) >= 0) { best = Math.max(best, 4); }
      }
      for (var h = 0; h < row.nHeadings.length; h++) {
        if (row.nHeadings[h].indexOf(t) >= 0) { best = Math.max(best, 3); }
      }
      if (row.nSummary.indexOf(t) >= 0) { best = Math.max(best, 2); }
      if (!best) { return null; }
      total += best;
    }
    // 結果に示す当たり: 題名のほかの欄で、タグ、見出し、要約の順に最初に当たったもの（データの並びで最初）
    function any(s) { for (var k2 = 0; k2 < ts.length; k2++) { if (s.indexOf(ts[k2]) >= 0) { return true; } } return false; }
    for (var g2 = 0; g2 < row.nTags.length && !hit; g2++) {
      if (any(row.nTags[g2])) { hit = { field: "tag", text: row.tags[g2] }; }
    }
    for (var h2 = 0; h2 < row.nHeadings.length && !hit; h2++) {
      if (any(row.nHeadings[h2])) { hit = { field: "heading", text: row.headings[h2][1], id: row.headings[h2][0] }; }
    }
    if (!hit && row.summary && any(row.nSummary)) { hit = { field: "summary", text: row.summary }; }
    return { score: total, hit: hit };
  }

  function search(q) {
    if (!SEARCH) { return null; }
    var ts = terms(q);
    if (!ts.length) { return []; }
    var out = [];
    for (var k = 0; k < SEARCH.length; k++) {
      var row = SEARCH[k];
      var s = score(row, ts);
      if (!s) { continue; }
      out.push({ rel_path: row.id + ".md", title: row.title, type: row.type, tags: row.tags.slice(),
                 updated: row.updated, score: s.score, snippet: s.hit ? s.hit.text : row.summary,
                 heading_path: s.hit && s.hit.field === "heading" ? [s.hit.text] : [],
                 href: href(row.path) + (s.hit && s.hit.field === "heading" ? "#" + encodeURIComponent(s.hit.id) : ""),
                 hit: s.hit, id: row.id, group_path: row.group });
    }
    // 点の高い順、同じなら題名の文字（UTF-16 の符号の順。localeCompare は使わない）
    out.sort(function (a, b) {
      if (b.score !== a.score) { return b.score - a.score; }
      return a.title < b.title ? -1 : (a.title > b.title ? 1 : 0);
    });
    return out;
  }
  state.search = search;

  // 当たった文字に <mark> を付けて node に入れる。そろえた列の中で探し、元の文字の境界に戻す（5.3）
  function highlight(node, text, ts) {
    text = String(text || "").normalize("NFC");
    var chars = Array.from(text);
    var low = "";
    var starts = [];      // [そろえた列の位置, 元の文字列の位置]
    var acc = 0;
    for (var k = 0; k < chars.length; k++) {
      starts.push([low.length, acc]);
      low += foldChar(chars[k]);
      acc += chars[k].length;
    }
    starts.push([low.length, acc]);
    function back(pos) { for (var j = starts.length - 1; j >= 0; j--) { if (starts[j][0] <= pos) { return starts[j][1]; } } return 0; }
    function fwd(pos) { for (var j = 0; j < starts.length; j++) { if (starts[j][0] >= pos) { return starts[j][1]; } } return acc; }
    var marks = [];
    for (var m = 0; m < ts.length; m++) {
      var p = low.indexOf(ts[m]);
      if (p >= 0) { marks.push([back(p), fwd(p + ts[m].length)]); }
    }
    marks.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [];
    for (var n = 0; n < marks.length; n++) {
      var last = merged[merged.length - 1];
      if (last && marks[n][0] <= last[1]) { last[1] = Math.max(last[1], marks[n][1]); } else { merged.push([marks[n][0], marks[n][1]]); }
    }
    var pos = 0;
    for (var r = 0; r < merged.length; r++) {
      node.appendChild(document.createTextNode(text.slice(pos, merged[r][0])));
      node.appendChild(el("mark", null, text.slice(merged[r][0], merged[r][1])));
      pos = merged[r][1];
    }
    node.appendChild(document.createTextNode(text.slice(pos)));
  }

  /* --- 検索の索引を後から読む（5.4） --- */
  var waiting = [];
  var loadState = "idle";       // idle、loading、ready、failed
  var loadMessage = "";
  function withSearch(fn) {
    if (loadState === "ready" || loadState === "failed") { fn(); return; }
    waiting.push(fn);
    if (loadState === "loading") { return; }
    loadState = "loading";
    var s = document.createElement("script");
    s.src = root + "data/search.js";
    s.onload = function () {
      var raw = window.WIKI_SEARCH;
      if (!raw || raw.protocol_version !== PROTOCOL) {
        loadState = "failed";
        loadMessage = "この画面は、検索の索引の版に対応していない";
      } else {
        if (raw.generation !== generation) { state.mismatch.push("WIKI_SEARCH"); updatedNotice(); }
        SEARCH = prepare(raw);
        state.searchIncomplete = !!raw.incomplete;
        loadState = "ready";
        state.searchLoaded = true;
      }
      flush();
    };
    s.onerror = function () {
      loadState = "failed";
      loadMessage = "検索の索引を読めない";
      flush();
    };
    document.body.appendChild(s);
  }
  function flush() {
    if (loadState === "failed") { failSearch(); }
    var w = waiting;
    waiting = [];
    for (var k = 0; k < w.length; k++) { w[k](); }
  }
  state.loadSearch = withSearch;

  /* --- 検索欄（Fluent の SearchBox） --- */
  function searchBox(placeholder, onQuery, onEnter) {
    var box = el("span", "fui-SearchBox fui-Input");
    box.setAttribute("data-size", "small");
    var before = el("span", "fui-SearchBox__contentBefore fui-Input__contentBefore");
    before.appendChild(icon("search", "1em"));
    var input = el("input", "fui-SearchBox__input fui-Input__input");
    input.type = "search";
    input.id = "_q";
    input.autocomplete = "off";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", placeholder);
    var after = el("span", "fui-SearchBox__contentAfter fui-Input__contentAfter");
    var dismiss = el("span", "fui-SearchBox__dismiss");
    dismiss.setAttribute("role", "button");
    dismiss.setAttribute("aria-label", "消去");
    dismiss.tabIndex = -1;
    dismiss.appendChild(icon("dismiss", "1em"));
    // 消去ボタンを押してもフォーカスを外さない（fluent2.js の代わり）
    dismiss.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
    dismiss.addEventListener("click", function () { input.value = ""; onQuery(""); input.focus(); });
    after.appendChild(dismiss);
    box.appendChild(before);
    box.appendChild(input);
    box.appendChild(after);
    input.addEventListener("focus", function () { withSearch(function () { onQuery(input.value); }); });
    input.addEventListener("input", function () { onQuery(input.value); });
    input.addEventListener("keydown", function (ev) {
      if (ev.isComposing || ev.keyCode === 229) { return; }    // かな漢字の変換中のキーは扱わない
      if (ev.key === "Enter") { onEnter(); } else if (ev.key === "Escape") { input.value = ""; onQuery(""); }
    });
    return { box: box, input: input };
  }

  document.addEventListener("keydown", function (ev) {
    var q = document.getElementById("_q");
    var a = document.activeElement;
    if (ev.key === "/" && q && a !== q && !(a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName))) {
      ev.preventDefault();
      q.focus();
    }
  });

  /* --- 左の木 --- */
  function groupLink(g) {
    var a = el("a", "fui-Link tree-filter", "一覧");
    a.href = href("index.html") + "#group=" + encodeURIComponent(g.id);
    return a;
  }

  function containsCurrent(g) {
    for (var k = 0; k < g.items.length; k++) { if (g.items[k].state === "resolved" && g.items[k].id === current) { return true; } }
    for (var c = 0; c < g.children.length; c++) { if (containsCurrent(g.children[c])) { return true; } }
    return false;
  }

  function treeItem(it) {
    if (it.state === "resolved") {
      var a = el("a", "fui-NavSubItem", it.label);
      a.href = href(it.path) + (it.anchor ? "#" + encodeURIComponent(it.anchor) : "");
      var here = it.id === current;
      a.setAttribute("aria-current", here ? "page" : "false");
      if (here) { a.classList.add("is-current"); }
      return a;
    }
    if (it.state === "group") {
      var g = el("a", "fui-NavSubItem", it.label);
      g.href = href("index.html") + "#group=" + encodeURIComponent(it.group);
      return g;
    }
    var span = el("span", "fui-NavSubItem wl wl-" + it.state, it.label);
    span.title = it.state === "unresolved" ? "ページがない" : "行き先を 1 つに決められない";
    return span;
  }

  function treeGroup(g, depth) {
    var d = el("details", "fui-NavCategory tree-group nav-depth-" + Math.min(depth, 6));
    d.open = containsCurrent(g);
    var sum = el("summary", "fui-NavCategoryItem");
    sum.appendChild(el("span", "tree-name", g.title));
    sum.appendChild(el("span", "fui-Caption1 tree-count", String(g.count)));
    sum.appendChild(groupLink(g));
    var exp = el("span", "fui-NavCategoryItem__expandIcon");
    exp.setAttribute("aria-hidden", "true");
    exp.appendChild(icon("chevron"));
    sum.appendChild(exp);
    d.appendChild(sum);
    var sub = el("div", "fui-NavSubItemGroup");
    for (var k = 0; k < g.items.length; k++) { sub.appendChild(treeItem(g.items[k])); }
    for (var c = 0; c < g.children.length; c++) { sub.appendChild(treeGroup(g.children[c], depth + 1)); }
    d.appendChild(sub);
    return d;
  }

  function indexTree(groups, depth) {
    var ul = el("ul", "tree-groups nav-depth-" + Math.min(depth, 6));
    for (var k = 0; k < groups.length; k++) {
      var g = groups[k];
      var li = el("li");
      var a = el("a", "fui-NavItem");
      a.href = "#group=" + encodeURIComponent(g.id);
      a.setAttribute("data-group", g.id);
      a.appendChild(el("span", "tree-name", g.title));
      a.appendChild(el("span", "fui-Caption1 tree-count", String(g.count)));
      li.appendChild(a);
      if (g.children.length) { li.appendChild(indexTree(g.children, depth + 1)); }
      ul.appendChild(li);
    }
    return ul;
  }

  var groupIds = {};
  var groupTitles = {};
  (function collect(groups) {
    for (var k = 0; k < groups.length; k++) {
      groupIds[groups[k].id] = true;
      groupTitles[groups[k].id] = groups[k].title;
      collect(groups[k].children);
    }
  })(NAV.groups || []);

  var drawer = el("details", "drawer");
  var drawerSum = el("summary", "fui-Button drawer-toggle", "メニュー");
  drawerSum.setAttribute("data-appearance", "subtle");
  drawer.appendChild(drawerSum);
  var tree = el("div", "tree");

  if (isIndex) {
    var all = el("a", "fui-NavItem tree-all");
    all.href = "#";
    all.setAttribute("data-group", "");
    all.appendChild(el("span", "tree-name", "すべてのページ"));
    tree.appendChild(all);
    tree.appendChild(indexTree(NAV.groups || [], 0));
  } else {
    // 記事のページ: 検索欄と、その結果の場所（木の場所に出す）
    var results = el("div", "sr-area");
    results.hidden = true;
    var field = el("div", "search");
    var hint = el("p", "fui-Caption1 search-hint", "/ で検索欄に移る。Enter で最初の結果を開く");
    var sb = searchBox("ページを探す", function (q) { showResults(q); }, function () {
      var first = results.querySelector("a.sr-link");
      if (first && !results.hidden) { location.href = first.href; }
    });
    field.appendChild(sb.box);
    field.appendChild(hint);
    navBody.appendChild(field);
    navBody.appendChild(results);
    for (var n = 0; n < (NAV.groups || []).length; n++) { tree.appendChild(treeGroup(NAV.groups[n], 0)); }
  }
  drawer.appendChild(tree);
  navBody.appendChild(drawer);

  function showResults(q) {
    var ts = terms(q);
    if (!ts.length) {
      results.hidden = true;
      tree.hidden = false;
      return;
    }
    if (loadState !== "ready") {
      if (loadState === "failed") { failSearch(); }
      return;
    }
    var found = search(q);
    while (results.firstChild) { results.removeChild(results.firstChild); }
    results.appendChild(el("p", "fui-Caption1 sr-count", found.length + " 件"));
    if (!found.length) { results.appendChild(el("p", "fui-Caption1 sr-empty", "当たるページはない")); }
    var ul = el("ul", "fui-List sr-results");
    ul.setAttribute("role", "list");
    found.slice(0, 30).forEach(function (f) {
      var li = el("li", "fui-ListItem");
      var a = el("a", "sr-link");
      a.href = f.href;
      var title = el("span", "sr-title");
      highlight(title, f.title, ts);
      a.appendChild(title);
      var meta = el("span", "fui-Caption1 sr-meta");
      meta.appendChild(el("span", "badge fui-Badge " + typeClass(f.type), typeLabel(f.type)));
      if (f.group_path.length) { meta.appendChild(document.createTextNode(" " + f.group_path.join(" › "))); }
      a.appendChild(meta);
      if (f.hit) {
        var names = { tag: "タグ", heading: "見出し", summary: "要約" };
        var hitEl = el("span", "fui-Caption1 sr-hit", names[f.hit.field] + ": ");
        highlight(hitEl, f.hit.text, ts);
        a.appendChild(hitEl);
      }
      li.appendChild(a);
      ul.appendChild(li);
    });
    results.appendChild(ul);
    if (state.searchIncomplete) {
      results.appendChild(el("p", "fui-Caption1 sr-incomplete", "読めないページがあるので、結果は不完全"));
    }
    results.hidden = false;
    tree.hidden = true;
  }

  // 検索の索引を使えないとき（読めない、版が違う）。欄を使えなくし、理由を欄の下に出す（5.5）
  function failSearch() {
    var q = document.getElementById("_q");
    if (q) { q.disabled = true; }
    var h = document.querySelector(".search-hint, .fx-hint");
    if (h) {
      h.textContent = loadMessage;
      h.hidden = false;
      h.classList.add("search-error");
    }
  }

  // 広い幅では「メニュー」を開いておき、summary は CSS で隠す（3.3）
  var narrow = window.matchMedia ? window.matchMedia("(max-width: 48rem)") : null;
  function syncDrawer() { if (!narrow || !narrow.matches) { drawer.open = true; } }
  syncDrawer();
  if (narrow && narrow.addEventListener) { narrow.addEventListener("change", syncDrawer); }

  var cur = tree.querySelector(".is-current");
  if (cur && cur.scrollIntoView) {
    // 木だけを送る。ページ全体は送らない
    var box = navBody.getBoundingClientRect();
    var r = cur.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) { navBody.scrollTop += r.top - box.top - box.height / 3; }
  }

  /* --- 索引のページの絞り込みと並べ替え（4.3） --- */
  if (isIndex) {
    var list = document.getElementById("_index");
    var rows = Array.prototype.slice.call(list.querySelectorAll(".fx-row"));
    var bar = document.getElementById("_filters");
    var countEl = document.getElementById("_count");
    var filt = { q: "", type: null, tag: null, group: "" };
    var noscript = bar.querySelector("noscript");
    if (noscript) { bar.removeChild(noscript); }

    var fsb = searchBox("題名、タグ、見出し、要約で絞る", function (q) { filt.q = q; apply(); }, function () {
      var first = rows.filter(function (r) { return !r.hidden; })[0];
      if (first) { location.href = first.querySelector(".ix-title").href; }
    });
    var fhint = el("p", "fui-Caption1 fx-hint");
    fhint.hidden = true;

    // 種類のボタン。種類は Map で集める（__proto__ のような値も、ふつうの種類として扱う）
    var types = new Map();
    rows.forEach(function (r) { var t = r.getAttribute("data-type") || ""; types.set(t, (types.get(t) || 0) + 1); });
    var chips = el("div", "chips");
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "種類");
    function chip(label, value) {
      var b = el("button", "fui-ToggleButton fui-Button", label);
      b.type = "button";
      b.setAttribute("data-size", "small");
      b.setAttribute("data-shape", "circular");
      b.setAttribute("aria-pressed", value === null ? "true" : "false");
      b.addEventListener("click", function () {
        filt.type = value;
        Array.prototype.forEach.call(chips.children, function (c) { c.setAttribute("aria-pressed", c === b ? "true" : "false"); });
        apply();
      });
      chips.appendChild(b);
    }
    chip("すべて", null);
    types.forEach(function (_n, t) { chip(typeLabel(t), t); });

    // タグの選択。タグは行の span.tag の文字から読み、Map で数える
    var tagCount = new Map();
    rows.forEach(function (r) {
      var seen = new Set();
      Array.prototype.forEach.call(r.querySelectorAll("span.tag"), function (s) {
        var t = s.textContent;
        if (seen.has(t)) { return; }
        seen.add(t);
        tagCount.set(t, (tagCount.get(t) || 0) + 1);
      });
    });
    var tagBox = el("span", "fui-Select tag-select");
    tagBox.setAttribute("data-size", "small");
    var tagSel = el("select", "fui-Select__select");
    tagSel.setAttribute("aria-label", "タグ");
    var optAll = el("option", "", "タグ: すべて");
    optAll.value = "";
    tagSel.appendChild(optAll);
    var tagNames = Array.from(tagCount.keys()).sort(function (a, b) { return a < b ? -1 : (a > b ? 1 : 0); });
    tagNames.forEach(function (t, k) {
      var o = el("option", "", t + "（" + tagCount.get(t) + "）");
      o.value = String(k + 1);
      tagSel.appendChild(o);
    });
    tagSel.addEventListener("change", function () { filt.tag = tagSel.value ? tagNames[Number(tagSel.value) - 1] : null; apply(); });
    var tagIc = el("span", "fui-Select__icon");
    tagIc.appendChild(icon("chevron", "1em"));
    tagBox.appendChild(tagSel);
    tagBox.appendChild(tagIc);

    var sortBox = el("span", "fui-Select sort-select");
    sortBox.setAttribute("data-size", "small");
    var sortSel = el("select", "fui-Select__select");
    sortSel.setAttribute("aria-label", "並べ方");
    [["order", "索引の順"], ["updated", "更新の新しい順"], ["title", "題名の順"]].forEach(function (o) {
      var op = el("option", "", o[1]);
      op.value = o[0];
      sortSel.appendChild(op);
    });
    sortSel.addEventListener("change", apply);
    var sortIc = el("span", "fui-Select__icon");
    sortIc.appendChild(icon("chevron", "1em"));
    sortBox.appendChild(sortSel);
    sortBox.appendChild(sortIc);

    var groupChip = el("button", "fui-Button group-chip");
    groupChip.type = "button";
    groupChip.setAttribute("data-size", "small");
    groupChip.setAttribute("data-shape", "circular");
    groupChip.hidden = true;
    groupChip.addEventListener("click", function () { history.replaceState(null, "", location.pathname + location.search); fromHash(); });

    bar.appendChild(fsb.box);
    bar.appendChild(chips);
    bar.appendChild(tagBox);
    bar.appendChild(sortBox);
    bar.appendChild(groupChip);
    bar.appendChild(fhint);

    var byId = {};
    function rowMatches(r) {
      if (filt.type !== null && (r.getAttribute("data-type") || "") !== filt.type) { return false; }
      if (filt.group && (" " + r.getAttribute("data-groups") + " ").indexOf(" " + filt.group + " ") < 0) { return false; }
      if (filt.tag !== null) {
        var ok = Array.prototype.some.call(r.querySelectorAll("span.tag"), function (s) { return s.textContent === filt.tag; });
        if (!ok) { return false; }
      }
      var ts = terms(filt.q);
      if (ts.length) {
        if (loadState !== "ready") { return true; }       // 読み終わったときに照らし直す
        var row = byId[r.getAttribute("data-id")];
        if (!row || !score(row, ts)) { return false; }
      }
      return true;
    }

    function apply() {
      if (terms(filt.q).length && loadState === "idle") { withSearch(apply); }
      if (loadState === "ready" && SEARCH && !Object.keys(byId).length) {
        SEARCH.forEach(function (row) { byId[row.id] = row; });
      }
      var shown = 0;
      rows.forEach(function (r) {
        var ok = rowMatches(r);
        r.hidden = !ok;
        if (ok) { shown++; }
      });
      var key = sortSel.value;
      function order(r) { return Number(r.getAttribute("data-order")); }
      var sorted = rows.slice().sort(function (a, b) {
        if (key === "updated") {
          var ua = a.getAttribute("data-updated") || "";
          var ub = b.getAttribute("data-updated") || "";
          if (!ua !== !ub) { return ua ? -1 : 1; }      // 値のない行は最後
          if (ua !== ub) { return ua > ub ? -1 : 1; }
        } else if (key === "title") {
          var c = a.getAttribute("data-title").localeCompare(b.getAttribute("data-title"), "ja");
          if (c) { return c; }
        }
        return order(a) - order(b);
      });
      sorted.forEach(function (r) { list.appendChild(r); });
      var filtered = filt.q || filt.type !== null || filt.tag !== null || filt.group;
      countEl.textContent = filtered ? rows.length + " 件中 " + shown + " 件" : rows.length + " 件";
      if (incompletePages || state.searchIncomplete) {
        countEl.appendChild(el("span", "count-incomplete", "（読めないページがあるので不完全）"));
      }
      Array.prototype.forEach.call(tree.querySelectorAll("[data-group]"), function (a) {
        var on = a.getAttribute("data-group") === filt.group;
        a.setAttribute("aria-current", on ? "true" : "false");
        a.classList.toggle("is-current", on);
      });
      groupChip.hidden = !filt.group;
      groupChip.textContent = filt.group ? "グループ: " + groupTitles[filt.group] + " ×" : "";
    }

    function fromHash() {
      var m = /^#group=(.+)$/.exec(location.hash);
      var id = m ? decodeURIComponent(m[1]) : "";
      filt.group = GROUP_ID.test(id) && has(groupIds, id) ? id : "";
      apply();
    }
    window.addEventListener("hashchange", fromHash);
    all.addEventListener("click", function (ev) {
      ev.preventDefault();
      history.replaceState(null, "", location.pathname + location.search);
      fromHash();
    });
    fromHash();
  }
  state.navLoaded = true;
})();
