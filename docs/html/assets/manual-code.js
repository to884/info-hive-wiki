(() => {
  "use strict";

  const shellStart = /^[ \t]*(?:\/(?:wiki:[a-z-]+|(?:plugin|mcp)\b)|\$wiki:info-hive|\$[\w:]+\s*=|(?:git|uv|python|winget|codex|powershell(?:\.exe)?|curl|irm|npx|bash|cd|mkdir|ln|grep|cp)\b|(?:Add|Get|Set|Start|Stop|Invoke|Join|Select|Test|New|Remove|Write|Measure)-[A-Z][\w-]*)/m;
  const shellTokens = /(?<comment>^[ \t]*#[^\r\n]*)|(?<string>"(?:\\.|[^"\\\r\n])*"|'[^'\r\n]*')|(?<keyword>\/(?:wiki:[a-z-]+|(?:plugin|mcp)\b)|\$wiki:info-hive|\[[a-z][\w.]*\]::[a-z]\w*|\b(?:(?:Add|Get|Set|Start|Stop|Invoke|Join|Select|Test|New|Remove|Write|Measure)-[A-Z][\w-]*|git|uv|python|winget|codex|powershell(?:\.exe)?|curl|irm|npx|bash|cd|mkdir|ln|grep|cp|if|then|else|fi|for|in|do|done)\b)|(?<option>(?<![\w.-])--?[a-z][\w-]*)|(?<variable>\$[a-z_][\w:]*)/gim;
  const dataTokens = /(?<comment>^[ \t]*#[^\r\n]*)|(?<key>"(?:\\.|[^"\\\r\n])*"(?=[ \t]*:)|^[ \t]*[a-z_][\w-]*(?=[ \t]*:))|(?<string>"(?:\\.|[^"\\\r\n])*"|'[^'\r\n]*')|(?<keyword>\b(?:true|false|null)\b)|(?<number>(?<![\w.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\b)/gim;
  const diagramTokens = /(?<comment>%%[^\r\n]*)|(?<string>"[^"\r\n]*")|(?<keyword>\b(?:flowchart|graph|sequenceDiagram|participant|actor|subgraph|end|LR|RL|TD|TB|BT)\b)|(?<option>-->|-\.->|==>|->>)/gm;
  // @fluentui/svg-icons 1.1.345 の Copy 16（MIT、Microsoft Corporation）。
  const copyIcons = {
    regular: "M5 6H3.5C2.67 6 2 6.67 2 7.5v5c0 .83.67 1.5 1.5 1.5h4c.83 0 1.5-.67 1.5-1.5V12h1v.5A2.5 2.5 0 0 1 7.5 15h-4A2.5 2.5 0 0 1 1 12.5v-5A2.5 2.5 0 0 1 3.5 5H5zm7.5-5A2.5 2.5 0 0 1 15 3.5v5a2.5 2.5 0 0 1-2.5 2.5h-4A2.5 2.5 0 0 1 6 8.5v-5A2.5 2.5 0 0 1 8.5 1zm-4 1C7.67 2 7 2.67 7 3.5v5c0 .83.67 1.5 1.5 1.5h4c.83 0 1.5-.67 1.5-1.5v-5c0-.83-.67-1.5-1.5-1.5z",
    filled: "M5 8.5A3.5 3.5 0 0 0 8.5 12H10v.5A2.5 2.5 0 0 1 7.5 15h-4A2.5 2.5 0 0 1 1 12.5v-5A2.5 2.5 0 0 1 3.5 5H5zM12.5 1A2.5 2.5 0 0 1 15 3.5v5a2.5 2.5 0 0 1-2.5 2.5h-4A2.5 2.5 0 0 1 6 8.5v-5A2.5 2.5 0 0 1 8.5 1z",
  };

  function grammar(code, source) {
    const language = [...code.classList].find((name) => name.startsWith("language-"));
    switch (language) {
      case "language-json":
      case "language-yaml":
        return dataTokens;
      case "language-mermaid":
        return diagramTokens;
      case "language-bash":
      case "language-sh":
      case "language-powershell":
        return shellTokens;
      default:
        return shellStart.test(source) ? shellTokens : null;
    }
  }

  function highlight(code, source) {
    const tokens = grammar(code, source);
    if (!tokens) return;
    const fragment = document.createDocumentFragment();
    let offset = 0;
    for (const match of source.matchAll(tokens)) {
      const kind = Object.keys(match.groups).find((key) => match.groups[key] !== undefined);
      const span = document.createElement("span");
      span.className = `code-token-${kind}`;
      span.textContent = match[0];
      fragment.append(source.slice(offset, match.index), span);
      offset = match.index + match[0].length;
    }
    fragment.append(source.slice(offset));
    code.replaceChildren(fragment);
  }

  async function copy(source, button) {
    try {
      await navigator.clipboard.writeText(source);
      return true;
    } catch {
      // ローカルファイルを開いたときなどに、選択範囲からのコピーを試す。
      const field = document.createElement("textarea");
      field.className = "code-copy-source";
      field.value = source;
      field.readOnly = true;
      document.body.append(field);
      field.select();
      try {
        return document.execCommand("copy");
      } catch {
        return false;
      } finally {
        field.remove();
        button.focus({preventScroll: true});
      }
    }
  }

  function copyIcon() {
    const slot = document.createElement("span");
    slot.className = "fui-Button__icon";
    slot.setAttribute("aria-hidden", "true");
    const svgNamespace = "http://www.w3.org/2000/svg";
    for (const [variant, data] of Object.entries(copyIcons)) {
      const svg = document.createElementNS(svgNamespace, "svg");
      svg.setAttribute("class", `fui-Icon fui-Icon-${variant}`);
      svg.setAttribute("viewBox", "0 0 16 16");
      svg.setAttribute("width", "16");
      svg.setAttribute("height", "16");
      svg.setAttribute("fill", "currentColor");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS(svgNamespace, "path");
      path.setAttribute("d", data);
      svg.append(path);
      slot.append(svg);
    }
    return slot;
  }

  function addCopy(code, source) {
    const pre = code.parentElement;
    const example = document.createElement("div");
    const toolbar = document.createElement("div");
    const status = document.createElement("span");
    const button = document.createElement("button");
    example.className = "code-example";
    toolbar.className = "code-toolbar";
    status.className = "code-copy-status";
    status.setAttribute("role", "status");
    button.className = "fui-Button";
    button.dataset.appearance = "subtle";
    button.dataset.size = "small";
    button.dataset.iconOnly = "";
    button.type = "button";
    button.title = "コードをコピー";
    button.setAttribute("aria-label", button.title);
    button.append(copyIcon());
    button.addEventListener("click", async () => {
      status.textContent = "";
      status.textContent = await copy(source, button) ? "コピーしました" : "コピーできませんでした";
    });
    pre.before(example);
    toolbar.append(status, button);
    example.append(toolbar, pre);
  }

  for (const code of document.querySelectorAll("pre > code")) {
    const source = code.textContent;
    highlight(code, source);
    addCopy(code, source);
  }
})();
