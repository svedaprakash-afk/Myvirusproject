// Inlines dist-demo/demo.js + demo.css into one HTML fragment suitable for publishing as a single-file page.
import { readFileSync, writeFileSync } from "node:fs";
const [, , out] = process.argv;
const js = readFileSync("dist-demo/demo.js", "utf8").replace(/<\/script/gi, "<\\/script");
const css = readFileSync("dist-demo/demo.css", "utf8");
const html = `<title>Cash Application Demo</title>
<style>${css}</style>
<div id="root"></div>
<script type="module">${js}</script>
`;
writeFileSync(out, html);
console.log("wrote", out, (html.length / 1e6).toFixed(2), "MB");
