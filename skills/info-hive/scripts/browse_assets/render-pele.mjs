// 静的の書き出し専用。標準入力には図の原稿だけを受け取り、生成した画像を標準出力へ返す。
import { stdin, stdout } from "node:process";
import { render } from "./pele.mjs";

const FONT_FAMILY = '"Noto Sans JP", "Yu Gothic", Meiryo, sans-serif';
const OPTIONS = {
  maxWidth: 334, padding: 10, fontFamily: FONT_FAMILY, responsive: false,
  idPrefix: "wiki-pele", links: false, images: false, outputLimit: 500_000,
};
const THEMES = [
  { bg: "#ffffff", fg: "#242424", muted: "#616161", line: "#424242",
    surface: "#edf3f7", "surface-alt": "#e1eaf1", border: "#667e90", accent: "#176284" },
  { bg: "#1f1f1f", fg: "#f5f5f5", muted: "#bbbbbb", line: "#adbfcd",
    surface: "#263746", "surface-alt": "#2c4557", border: "#879fb1", accent: "#78c4e5" },
];

function withTheme(svg, palette) {
  const variables = Object.entries(palette).map(([name, value]) => `--pele-${name}:${value}`).join(";");
  const style = `${variables};--pele-font:${FONT_FAMILY.replaceAll('"', "&quot;")};background-color:${palette.bg};`;
  return svg.replace(/^(<svg\s[^>]*?\sstyle=")/, `$1${style}`);
}

function renderFigure(source) {
  try {
    const { svg } = render(source, OPTIONS);
    const images = THEMES.map(palette => withTheme(svg, palette));
    return images.every(image => image !== svg) ? images : null;
  } catch {
    // 描けないときは、呼び出し側でコードを表示する。原稿をエラーに出さない。
    return null;
  }
}

stdin.setEncoding("utf8");
let input = "";
for await (const chunk of stdin) input += chunk;
stdout.write(JSON.stringify(JSON.parse(input).map(renderFigure)));
