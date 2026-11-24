/* wiki の閲覧（browse）の画面。classic なスクリプトだけを使い、fetch、ES モジュール、Web Worker を使わない。
   ページの HTML には本文、メタ欄、被リンク、関係表、診断、索引の一覧が描いてあるので、このスクリプトは
   ナビゲーションの木、検索、索引の一覧の絞り込み、配色の切り替え、記事の画像の拡大表示を組み立て、データの世代を確かめる。
   要素の style には触らない。記事の図のボタンと拡大用の dialog が変更する要素と属性は ADR-0030 に記す。
   部品のマークアップは Fluent 2 の設計（docs/plans/wiki-browse-fluent2-2026-11-14.md）の
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
    error: "M7.88 1.88a3 3 0 0 1 4.24 0l6 6a3 3 0 0 1 0 4.24l-6 6a3 3 0 0 1-4.24 0l-6-6a3 3 0 0 1 0-4.24zM13.1 6.9a.5.5 0 0 0-.7 0L10 9.3 7.6 6.9a.5.5 0 0 0-.7.7L9.3 10l-2.4 2.4a.5.5 0 0 0 .7.7l2.4-2.4 2.4 2.4a.5.5 0 0 0 .7-.7L10.7 10l2.4-2.4a.5.5 0 0 0 0-.7",
    // Tree の枝のシェブロン（ChevronRight12Regular）。viewBox は 12 × 12
    chevronRight: "M4.65 2.15a.5.5 0 0 0 0 .7L7.79 6 4.65 9.15a.5.5 0 1 0 .7.7l3.5-3.5a.5.5 0 0 0 0-.7l-3.5-3.5a.5.5 0 0 0-.7 0"
  };
  var ICON_BOX = { chevronRight: "0 0 12 12" };

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
    svg.setAttribute("viewBox", has(ICON_BOX, name) ? ICON_BOX[name] : "0 0 20 20");
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", ICONS[name]);
    svg.appendChild(p);
    return svg;
  }

  /* --- 記事の画像の拡大表示 --- */
  function initImagePreview() {
    var body = document.querySelector("#_article #_body");
    var dialog = el("dialog", "fui-DialogSurface wiki-image-preview");
    if (!body || !dialog.showModal) { return; }

    dialog.setAttribute("aria-label", "画像の拡大表示");
    var close = el("button", "fui-Button wiki-image-preview-close", "閉じる");
    close.type = "button";
    close.autofocus = true;
    var preview = el("img", "wiki-image-preview-img");
    dialog.append(close, preview);

    close.addEventListener("click", function () { dialog.close(); });
    function onBackdrop(ev) {
      if (ev.target !== dialog) { return false; }
      var bounds = dialog.getBoundingClientRect();
      return ev.clientX < bounds.left || ev.clientX >= bounds.right ||
             ev.clientY < bounds.top || ev.clientY >= bounds.bottom;
    }
    var backdropPressed = false;
    dialog.addEventListener("pointerdown", function (ev) { backdropPressed = onBackdrop(ev); });
    dialog.addEventListener("click", function (ev) {
      if (backdropPressed && onBackdrop(ev)) { dialog.close(); }
      backdropPressed = false;
    });
    dialog.addEventListener("close", function () {
      if (!dialog.open) { preview.removeAttribute("src"); }
    });

    var selector = ".wiki-fig > img";
    function addTrigger(img) {
      if (!img.complete || !img.naturalWidth || img.closest("a")) { return; }
      var figure = img.parentElement;
      var trigger = el("button", "wiki-image-trigger");
      trigger.type = "button";
      trigger.setAttribute("aria-haspopup", "dialog");
      trigger.setAttribute("aria-label", (img.alt.trim() || "画像") + "を拡大");
      var imageTheme = img.getAttribute("data-fig-theme");
      if (imageTheme) { trigger.setAttribute("data-fig-theme", imageTheme); }
      figure.insertBefore(trigger, img);
      trigger.appendChild(img);
      trigger.addEventListener("click", function () {
        if (dialog.open || !img.complete || !img.naturalWidth) { return; }
        var theme = imageTheme || figure.getAttribute("data-theme");
        if (theme) { dialog.setAttribute("data-theme", theme); }
        else { dialog.removeAttribute("data-theme"); }
        // SVG は拡大時も img として扱う。元の URL へは移動しない。
        preview.alt = img.alt;
        preview.src = img.currentSrc || img.src;
        if (!dialog.isConnected) { document.body.appendChild(dialog); }
        // ポインター操作でボタンにフォーカスしないブラウザーでも、閉じたときの復帰先を決める。
        trigger.focus({ preventScroll: true });
        backdropPressed = false;
        dialog.showModal();
      });
    }

    // サーバー版の Mermaid は後から画像を作る。ボタンで包んだあとも、figures.js は保持する img を使って描き直す。
    body.addEventListener("load", function (ev) {
      if (ev.target.matches(selector)) { addTrigger(ev.target); }
    }, true);
    body.querySelectorAll(selector).forEach(addTrigger);
  }
  initImagePreview();

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

  /* --- 配色の切り替え（Fluent の Switch。左のペインの木の設計の 7 節）。オンがダークで、theme.js が描く前に当てた値を表す --- */
  function themeField() {
    var api = window.__browseTheme;
    if (!api) { return null; }
    var osDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    function osTheme() { return osDark && osDark.matches ? "dark" : "light"; }
    function shown() {
      var t = document.documentElement.getAttribute("data-theme");
      return t === "dark" || t === "light" ? t : osTheme();
    }
    // 形は Fluent 2 のサイトの「Theme」のトグル（線の枠と丸のつまみ。オンでつまみが右に移り、三日月になる）
    var wrap = el("div", "theme-switch");
    var label = el("label", "fui-Label theme-label", "テーマ");
    label.htmlFor = "_theme";
    var box = el("span", "theme-toggle");
    var input = el("input", "theme-input");
    input.type = "checkbox";
    input.id = "_theme";
    input.setAttribute("role", "switch");
    // 読み上げの名前は、見えている「テーマ」を含み、オンが何かを表す
    input.setAttribute("aria-label", "ダークテーマ");
    var track = el("span", "theme-track");
    track.setAttribute("aria-hidden", "true");
    track.appendChild(el("span", "theme-knob"));
    box.appendChild(input);
    box.appendChild(track);
    wrap.appendChild(label);
    wrap.appendChild(box);
    function sync() { input.checked = shown() === "dark"; }
    sync();
    // 切り替えたあとの見た目が OS と同じなら auto（OS に合わせる）を、違えばその見た目を保存する
    input.addEventListener("change", function () {
      var want = input.checked ? "dark" : "light";
      api.save(want === osTheme() ? "auto" : want);
      sync();
    });
    // OS に合わせている間は、OS の配色の変化にトグルを合わせる
    if (osDark && osDark.addEventListener) { osDark.addEventListener("change", sync); }
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

  /* --- 左の木（Fluent 2 の Tree。左のペインの木の設計の 2 節、9 節） --- */
  // 項目の入れ物。どの項目も TreeItemLayout と __main を持つ（2.2）。a の項目の中は span にする
  function treeNode(tag, cls, level) {
    var item = el(tag, "fui-TreeItem" + (cls ? " " + cls : ""));
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-level", String(level));
    item.tabIndex = -1;
    if (level >= 5) { item.classList.add("tree-deep"); }     // 字下げは 4 段目で止める（2.3）
    var inner = tag === "div" ? "div" : "span";
    var layout = el(inner, "fui-TreeItemLayout");
    item.appendChild(layout);
    return { item: item, layout: layout, inner: inner };
  }

  function nodeMain(n, text, cls) {
    n.layout.appendChild(el(n.inner, "fui-TreeItemLayout__main" + (cls ? " " + cls : ""), text));
  }

  // 件数のバッジ（6.3）。読み上げには項目の aria-label で伝えるので、バッジは隠す
  function nodeCount(n, g) {
    var aside = el(n.inner, "fui-TreeItemLayout__aside");
    var b = el("span", "fui-Badge fui-CounterBadge tree-count", String(g.count));
    b.setAttribute("data-appearance", "tint");
    b.setAttribute("data-color", "informative");
    b.setAttribute("data-size", "small");
    b.setAttribute("data-shape", "circular");
    b.setAttribute("aria-hidden", "true");
    b.title = g.count + " ページ";
    aside.appendChild(b);
    n.layout.appendChild(aside);
    n.item.setAttribute("aria-label", g.title + "（" + g.count + " ページ）");
  }

  // グループの一覧への行き先。Index のページでは断片だけ（右の一覧を絞る）、記事のページでは Index のページ
  function groupHref(id) { return (isIndex ? "" : href("index.html")) + "#group=" + encodeURIComponent(id); }

  // 記事ページの Tab の入口とスクロール位置には、従来の先頭タグのグループを使う
  var currentPrimaryGroup = null;

  // 木は、記事のページも Index のページも、グループの名前と件数だけを描く（記事の題名は描かない。9 節）。
  // 子のないグループは、グループの一覧へのリンクの葉にする。子を持つグループは枝にする
  function groupNode(g, level) {
    if (!g.children.length) {
      var n = treeNode("a", "tree-index", level);
      n.item.href = groupHref(g.id);
      n.item.setAttribute("data-group", g.id);
      nodeMain(n, g.title, "tree-name");
      nodeCount(n, g);
      var currentItem = !isIndex && g.items.find(function (it) { return it.state === "resolved" && it.id === current; });
      if (currentItem) {
        n.item.setAttribute("aria-current", "true");
        n.item.classList.add("is-current");
        if (!currentItem.secondary_tag && !currentPrimaryGroup) { currentPrimaryGroup = n.item; }
      }
      return n.item;
    }
    var b = treeNode("div", "tree-group", level);
    b.item.setAttribute("data-group", g.id);
    b.item.setAttribute("aria-expanded", level === 1 ? "true" : "false");
    var exp = el("div", "fui-TreeItemLayout__expandIcon");
    exp.setAttribute("aria-hidden", "true");
    exp.appendChild(icon("chevronRight", "12"));
    b.layout.appendChild(exp);
    nodeMain(b, g.title, "tree-name");
    nodeCount(b, g);
    var sub = el("div", "fui-Tree");
    sub.setAttribute("role", "group");
    b.item.appendChild(sub);
    for (var c = 0; c < g.children.length; c++) { sub.appendChild(groupNode(g.children[c], level + 1)); }
    return b.item;
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
  var treeRoot = el("div", "fui-Tree");
  treeRoot.setAttribute("role", "tree");
  treeRoot.setAttribute("aria-label", "Index の木");
  tree.appendChild(treeRoot);

  if (isIndex) {
    var allNode = treeNode("a", "tree-all", 1);
    var all = allNode.item;
    all.href = "#";
    all.setAttribute("data-group", "");
    nodeMain(allNode, "すべてのページ", "tree-name");
    treeRoot.appendChild(all);
    for (var gi = 0; gi < (NAV.groups || []).length; gi++) { treeRoot.appendChild(groupNode(NAV.groups[gi], 1)); }
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
    for (var n = 0; n < (NAV.groups || []).length; n++) { treeRoot.appendChild(groupNode(NAV.groups[n], 1)); }
    // 今のページを持つグループの祖先の枝を開く。ページが 2 つのグループに載っていれば、どちらも開く
    Array.prototype.forEach.call(treeRoot.querySelectorAll(".fui-TreeItem.is-current"), function (c) {
      for (var p = c.parentElement.closest(".fui-TreeItem"); p; p = p.parentElement.closest(".fui-TreeItem")) {
        p.setAttribute("aria-expanded", "true");
      }
    });
  }
  // 読めない索引のファイルがあるとき、木が不完全であることを示す（構造の直しの設計の 4.1、10）
  if (NAV.incomplete) {
    tree.insertBefore(el("p", "fui-Caption1 nav-incomplete", "読めない Index のファイルがあるので、この木は不完全"),
                      tree.firstChild);
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

  /* --- 木の操作（左のペインの木の設計の 2.4。fluent2.js の enhanceTree を下敷きにし、葉の Enter と同期を直した） --- */
  function treeItems() { return Array.prototype.slice.call(treeRoot.querySelectorAll(".fui-TreeItem")); }
  function isBranch(item) { return item.hasAttribute("aria-expanded"); }
  function parentItem(item) { return item.parentElement ? item.parentElement.closest(".fui-TreeItem") : null; }
  function firstChild(item) { return item.querySelector(":scope > .fui-Tree > .fui-TreeItem"); }
  function isShown(item) {
    for (var p = parentItem(item); p; p = parentItem(p)) { if (p.getAttribute("aria-expanded") === "false") { return false; } }
    return true;
  }
  function shownItems() { return treeItems().filter(isShown); }

  // ローヴィング: tabindex="0" の項目をちょうど 1 つにする
  function roving(item) {
    treeItems().forEach(function (t) { t.tabIndex = t === item ? 0 : -1; });
  }
  function rovingToCurrent() {
    var c = currentPrimaryGroup || treeRoot.querySelector(".fui-TreeItem.is-current");
    var shown = shownItems();
    roving(c && isShown(c) ? c : shown[0] || null);
  }
  function setOpen(item, open) {
    if (!isBranch(item)) { return; }
    item.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { return; }
    // 閉じた枝の中にフォーカスか Tab の入口があれば、枝に移す（隠れた項目を入口に残さない）
    var r = treeRoot.querySelector('.fui-TreeItem[tabindex="0"]');
    if (item.contains(document.activeElement) && document.activeElement !== item) { item.focus(); }
    else if (r && r !== item && item.contains(r)) { roving(item); }
  }
  function openAncestors(item) {
    for (var p = parentItem(item); p; p = parentItem(p)) { p.setAttribute("aria-expanded", "true"); }
  }
  // 枝を選ぶ: そのグループの一覧に移る。Index のページでは右の一覧を絞り、閉じていれば開く。
  // 記事のページでは、Index のページをそのグループに絞って開く（9 節）
  function selectGroup(item) {
    var id = item.getAttribute("data-group");
    if (isIndex) {
      setOpen(item, true);
      location.hash = "#group=" + id;
    } else {
      location.href = groupHref(id);
    }
  }

  // 同期はフォーカスが移ったことで行う。矢印キー、押す操作、focus() のどれでもここを通る
  treeRoot.addEventListener("focusin", function (ev) {
    var item = ev.target.closest(".fui-TreeItem");
    if (item && treeRoot.contains(item)) { roving(item); }
  });

  treeRoot.addEventListener("click", function (ev) {
    var layout = ev.target.closest(".fui-TreeItemLayout");
    if (!layout) { return; }
    var item = layout.parentElement;
    if (!item.classList.contains("fui-TreeItem")) { return; }
    // 葉はリンクの既定の動きに任せる。WebKit はリンクを押してもフォーカスを移さないので、ここで移し、
    // ローヴィングの同期（focusin）を通す（Tab の入口を押した葉にする）
    if (item.tagName !== "DIV") { item.focus(); return; }
    // 枝の名前は、そのグループの一覧に移る。シェブロンは開閉だけをする
    if (isBranch(item) && !ev.target.closest(".fui-TreeItemLayout__expandIcon")) {
      item.focus();
      selectGroup(item);
      return;
    }
    setOpen(item, item.getAttribute("aria-expanded") !== "true");
    item.focus();
  });

  treeRoot.addEventListener("keydown", function (ev) {
    if (ev.isComposing || ev.keyCode === 229) { return; }        // かな漢字の変換中のキーは扱わない
    if (ev.ctrlKey || ev.metaKey || ev.altKey) { return; }        // Ctrl+Enter などは既定の動き
    var item = ev.target;
    if (!item.classList || !item.classList.contains("fui-TreeItem")) { return; }
    var items = shownItems();
    var i = items.indexOf(item);
    var handled = true;
    if (ev.key === "ArrowDown") { if (items[i + 1]) { items[i + 1].focus(); } }
    else if (ev.key === "ArrowUp") { if (i > 0) { items[i - 1].focus(); } }
    else if (ev.key === "Home") { if (items[0]) { items[0].focus(); } }
    else if (ev.key === "End") { if (items.length) { items[items.length - 1].focus(); } }
    else if (ev.key === "ArrowRight") {
      if (isBranch(item)) {
        if (item.getAttribute("aria-expanded") !== "true") { setOpen(item, true); }
        else if (firstChild(item)) { firstChild(item).focus(); }
      }
    } else if (ev.key === "ArrowLeft") {
      if (isBranch(item) && item.getAttribute("aria-expanded") === "true") { setOpen(item, false); }
      else if (parentItem(item)) { parentItem(item).focus(); }
    } else if (ev.key === "Enter") {
      // 葉では止めない: リンクの葉はリンクの既定の動きで移る（1 回目のレビューの 1）
      if (item.tagName !== "DIV") { handled = false; }
      else if (isBranch(item)) { selectGroup(item); }
    } else if (ev.key.length === 1 && /\S/.test(ev.key) && ev.key !== "/") {
      // 先頭の文字での移動。「/」は検索欄に移るキーなので扱わない
      var key = ev.key.toLowerCase();
      var order = items.slice(i + 1).concat(items.slice(0, i + 1));
      for (var k = 0; k < order.length; k++) {
        var m = order[k].querySelector(":scope > .fui-TreeItemLayout > .fui-TreeItemLayout__main");
        if (m && m.textContent.trim().toLowerCase().indexOf(key) === 0) { order[k].focus(); break; }
      }
    } else { handled = false; }
    if (handled) { ev.preventDefault(); }
  });

  if (!isIndex) { rovingToCurrent(); }

  var cur = currentPrimaryGroup || tree.querySelector(".is-current");
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
    // serve は再起動でポートが変わる。サーバーが渡した識別子で分けて、タグと種類を別々の cookie に保存する。
    // 静的 HTML は書き出した世代で分けて localStorage に保存する。
    var filterKeys = JSON.parse(bar.getAttribute("data-filter-keys") || "{}");
    var filterKey = /^[0-9a-f]{32}$/;
    var serverView = filterKeys.server === true;
    var storageEnabled = filterKey.test(filterKeys.scope)
                         && (!serverView || (location.protocol === "http:" && location.hostname === "127.0.0.1"));
    var filterPrefix = "info-hive-browse-filter-" + filterKeys.scope + "-";
    function readFilter(name) {
      if (!storageEnabled) { return null; }
      var key = filterPrefix + name;
      try {
        var value;
        if (serverView) {
          var prefix = key + "=";
          var cookie = document.cookie.split(/;\s*/).find(function (part) { return part.startsWith(prefix); });
          value = cookie && cookie.slice(prefix.length);
        } else {
          value = window.localStorage.getItem(key);
        }
        return filterKey.test(value || "") ? value : null;
      } catch (e) { return null; }
    }
    function saveFilter(name, value) {
      if (!storageEnabled) { return; }
      var key = filterPrefix + name;
      var saved = filterKey.test(value || "") ? value : "";
      try {
        if (serverView) {
          document.cookie = key + "=" + saved + "; Path=/; Max-Age=" + (saved ? 31536000 : 0) + "; SameSite=Strict";
        } else if (saved) {
          window.localStorage.setItem(key, saved);
        } else {
          window.localStorage.removeItem(key);
        }
      } catch (e) { /* 保存できなくても、この索引の絞り込みは続ける。 */ }
    }
    var noscript = bar.querySelector("noscript");
    if (noscript) { bar.removeChild(noscript); }

    var fsb = searchBox("題名、タグ、見出し、要約で絞る", function (q) { filt.q = q; apply(); }, function () {
      // いま見えている先頭の行は、DOM の今の並び（並べ替えのあと）から読む。行の順は DOM だけが持つ
      var first = list.querySelector(".fx-row:not([hidden])");
      if (first) { location.href = first.querySelector(".ix-title").href; }
    });
    var fhint = el("p", "fui-Caption1 fx-hint");
    fhint.hidden = true;

    // 種類のボタン。種類は Map で集める（__proto__ のような値も、ふつうの種類として扱う）
    var types = new Map();
    var typeKeys = new Map(Object.entries(filterKeys.type || {}));
    rows.forEach(function (r) { var t = r.getAttribute("data-type") || ""; types.set(t, (types.get(t) || 0) + 1); });
    var chips = el("div", "chips");
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "種類");
    var chipList = [];                                  // [ボタン, 種類の値]。「すべて」の値は null
    function pressType(value) {
      chipList.forEach(function (c) { c[0].setAttribute("aria-pressed", c[1] === value ? "true" : "false"); });
    }
    function chip(label, value) {
      var b = el("button", "fui-ToggleButton fui-Button", label);
      b.type = "button";
      b.setAttribute("data-size", "small");
      b.setAttribute("data-shape", "circular");
      b.setAttribute("aria-pressed", value === null ? "true" : "false");
      b.addEventListener("click", function () {
        filt.type = value;
        saveFilter("type", typeKeys.get(value));
        pressType(value);
        // 種類とグループは、あとから選んだほうだけを効かせる（種類とグループの設計の 2 節）。
        // 「すべて」でない種類を選んだら、グループの絞り込みを外す。断片は replaceState で消すので hashchange は起きない
        if (value !== null && filt.group) {
          filt.group = "";
          history.replaceState(null, "", location.pathname + location.search);
        }
        apply();
      });
      chipList.push([b, value]);
      chips.appendChild(b);
    }
    chip("すべて", null);
    var savedType = readFilter("type");
    types.forEach(function (_n, t) {
      chip(typeLabel(t), t);
      if (savedType && savedType === typeKeys.get(t)) { filt.type = t; }
    });
    pressType(filt.type);

    // タグの選択。タグは行の span.tag の文字から読み、Map で数える
    var tagCount = new Map();
    var tagKeys = new Map(Object.entries(filterKeys.tag || {}));
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
    var savedTag = readFilter("tag");
    tagNames.forEach(function (t, k) {
      var o = el("option", "", t + "（" + tagCount.get(t) + "）");
      o.value = String(k + 1);
      tagSel.appendChild(o);
      if (savedTag && savedTag === tagKeys.get(t)) {
        filt.tag = t;
        tagSel.value = o.value;
      }
    });
    tagSel.addEventListener("change", function () {
      filt.tag = tagSel.value ? tagNames[Number(tagSel.value) - 1] : null;
      saveFilter("tag", tagKeys.get(filt.tag));
      apply();
    });
    var tagIc = el("span", "fui-Select__icon");
    tagIc.appendChild(icon("chevron", "1em"));
    tagBox.appendChild(tagSel);
    tagBox.appendChild(tagIc);

    var sortBox = el("span", "fui-Select sort-select");
    sortBox.setAttribute("data-size", "small");
    var sortSel = el("select", "fui-Select__select");
    sortSel.setAttribute("aria-label", "並べ方");
    [["order", "Index の順"], ["updated", "更新の新しい順"], ["title", "題名の順"]].forEach(function (o) {
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
      // 語で絞っているかは、照合と同じ terms() で決める（空白だけの欄は絞っていない。3 回目の実装のレビューの A）
      var filtered = terms(filt.q).length > 0 || filt.type !== null || filt.tag !== null || !!filt.group;
      countEl.textContent = filtered ? rows.length + " 件中 " + shown + " 件" : rows.length + " 件";
      if (incompletePages || state.searchIncomplete) {
        countEl.appendChild(el("span", "count-incomplete", "（読めないページがあるので不完全）"));
      }
      Array.prototype.forEach.call(treeRoot.querySelectorAll(".fui-TreeItem[data-group]"), function (a) {
        var on = a.getAttribute("data-group") === filt.group;
        a.setAttribute("aria-current", on ? "true" : "false");
        a.classList.toggle("is-current", on);
        if (on) { openAncestors(a); }                 // 絞っているグループの祖先の枝を開く（2.3）
      });
      groupChip.hidden = !filt.group;
      groupChip.textContent = filt.group ? "グループ: " + groupTitles[filt.group] + " ×" : "";
    }

    function fromHash() {
      // グループの ID は `_g` と英小文字か数字だけで、URL で符号化されない。断片は解かずに、そのまま形と木にあるかを
      // 確かめる（解くと、`%` のような壊れた断片で例外になり、前の絞り込みが残る。2 回目の実装のレビューの A）
      var m = /^#group=(.+)$/.exec(location.hash);
      var id = m ? m[1] : "";
      var next = GROUP_ID.test(id) && has(groupIds, id) ? id : "";
      // グループが別の値に変わったときだけ、種類を「すべて」に戻す（種類とグループの設計の 2 節）。
      // 同じグループを読み直したときや、グループを外したときは、選んだ種類を残す
      if (next && next !== filt.group && filt.type !== null) {
        filt.type = null;
        pressType(null);
      }
      filt.group = next;
      apply();
    }
    window.addEventListener("hashchange", fromHash);
    all.addEventListener("click", function (ev) {
      ev.preventDefault();
      history.replaceState(null, "", location.pathname + location.search);
      fromHash();
    });
    fromHash();
    // Tab の入口は、絞っているグループ（なければ「すべてのページ」）にする。木の中にフォーカスがあれば変えない
    if (!treeRoot.contains(document.activeElement)) { rovingToCurrent(); }
  }
  state.navLoaded = true;
})();
