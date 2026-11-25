// Verify that selections in the rendered body map to Unicode code point
// offsets, including a supplementary-plane character.
//
// Usage: node selection_test.js <rendered.html> <app.js>

const fs = require("fs");
const jsdomPath = process.env.JSDOM_PATH || "/tmp/jsdomtest/node_modules/jsdom";
const { JSDOM } = require(jsdomPath);

const [htmlPath, jsPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const appJs = fs.readFileSync(jsPath, "utf8");

const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/" });
const { window } = dom;
window.alert = (msg) => { throw new Error("alert: " + msg); };
window.eval(appJs);

const { document } = window;
const body = document.getElementById("body");
const start = document.getElementById("start");
const end = document.getElementById("end");

function textNodes() {
  const walker = document.createTreeWalker(body, window.NodeFilter.SHOW_TEXT);
  const out = [];
  while (walker.nextNode()) out.push(walker.currentNode);
  return out;
}

const nodes = textNodes();
const text = nodes.map((n) => n.data).join("");
const cpLength = (s) => Array.from(s).length;
console.log("BODY:", JSON.stringify(text), "utf8len", text.length, "cp", cpLength(text));
if (cpLength(text) !== Array.from(body.textContent).length) {
  throw new Error("body text mismatch");
}

function select(cpStart, cpEnd) {
  const sel = window.getSelection();
  sel.removeAllRanges();
  const range = document.createRange();
  range.setStart(body, 0);
  range.setEnd(body, 0);
  // Walk text nodes, converting code point offsets to UTF-16 offsets.
  let nodeStartCp = 0;
  let startNode = null, startOff = 0, endNode = null, endOff = 0;
  for (const node of nodes) {
    const nodeCp = cpLength(node.data);
    const nodeEndCp = nodeStartCp + nodeCp;
    if (startNode === null && cpStart >= nodeStartCp && cpStart <= nodeEndCp) {
      startNode = node;
      startOff = utf16Index(node.data, cpStart - nodeStartCp);
    }
    if (cpEnd >= nodeStartCp && cpEnd <= nodeEndCp) {
      endNode = node;
      endOff = utf16Index(node.data, cpEnd - nodeStartCp);
    }
    nodeStartCp = nodeEndCp;
  }
  range.setStart(startNode, startOff);
  range.setEnd(endNode, endOff);
  sel.addRange(range);
  document.dispatchEvent(new window.Event("selectionchange"));
  return window.__ocrSelection;
}

function utf16Index(s, cpOff) {
  return Array.from(s).slice(0, cpOff).join("").length;
}

const cases = [
  [0, 2],
  [1, 3],      // starts after 𠀀 boundary region
  [0, 7],      // crosses three spans
  [3, 4],
  [7, 8],      // page 2 box 1
];
let failures = 0;
for (const [a, b] of cases) {
  const got = select(a, b);
  const expectedText = Array.from(text).slice(a, b).join("");
  const ok = got && got.start === a && got.end === b &&
    start.value === String(a) && end.value === String(b) &&
    Array.from(document.getElementById("body").textContent).slice(got.start, got.end).join("") === expectedText;
  console.log(`select [${a},${b}) ->`, JSON.stringify(got), ok ? "OK" : "FAIL");
  if (!ok) failures += 1;
}
if (failures) {
  console.error(`${failures} selection case(s) failed`);
  process.exit(1);
}
console.log("JS_SELECTION_OK");
