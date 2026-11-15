/* 閲覧の画面の配色を、描く前に決める。<head> で同期で読む classic なスクリプトである。
   選んだ値は、http ではポートを区別しない cookie に、file:// では localStorage に置く。
   サーバーは起動ごとにポートが変わり、localStorage はポートごとに分かれるためである。
   読めない値や、読み書きの例外は、すべて "auto"（OS の設定に従う）として扱う。
   このスクリプトが変えるのは <html> の data-theme だけである。 */
(function () {
  "use strict";
  var KEY = "info-hive-browse-theme";
  var VALUES = { auto: true, light: true, dark: true };
  var isHttp = location.protocol === "http:" || location.protocol === "https:";

  function readCookie() {
    try {
      var parts = document.cookie ? document.cookie.split(";") : [];
      for (var i = 0; i < parts.length; i++) {
        var kv = parts[i].replace(/^\s+/, "").split("=");
        if (kv[0] === KEY) { return kv[1] || ""; }
      }
    } catch (e) { /* cookie を読めない */ }
    return "";
  }

  function readStorage() {
    try { return window.localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  function read() {
    var v = isHttp ? readCookie() : readStorage();
    return Object.prototype.hasOwnProperty.call(VALUES, v) ? v : "auto";
  }

  function save(value) {
    if (!Object.prototype.hasOwnProperty.call(VALUES, value)) { value = "auto"; }
    try {
      if (isHttp) {
        document.cookie = KEY + "=" + value + "; Path=/; Max-Age=31536000; SameSite=Strict";
      } else {
        window.localStorage.setItem(KEY, value);
      }
    } catch (e) { /* 保存できなくても、今のページの配色は変える */ }
    apply(value);
  }

  function apply(value) {
    document.documentElement.setAttribute("data-theme", value);
  }

  apply(read());
  window.__browseTheme = { read: read, save: save, values: ["auto", "light", "dark"] };
})();
