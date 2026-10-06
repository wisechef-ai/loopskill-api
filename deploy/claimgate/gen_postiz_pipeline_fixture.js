// Regenerate tests/fixtures/postiz_pipeline.json from Postiz's REAL converter.
//
// The modules come from the running postiz container (parse5 6.0.1,
// striptags 3.2.0, tslib) plus its compiled strip.html.validation.js; see
// deploy/claimgate/README.md "Regenerating the pipeline fixture".
//
//   node deploy/claimgate/gen_postiz_pipeline_fixture.js <dir-with-strip.js-and-node_modules> [N]
//
// Output: [[input, none, bold, markdown], ...] = stripHtmlValidation(mode,
// input) for Postiz's three text modes: "none" (plain text), "normal" with
// replaceBold (links become their href, <li> newlines deleted, bold/underline
// as Unicode) and "markdown". Property tested in tests/test_claims_contract.py:
// for every input the gate does NOT flag as unsupported markup, the gate's
// join / link / markdown readings equal these outputs (after the same text
// normalisation).
"use strict";
const path = require("path");
const dir = path.resolve(process.argv[2]);
const { stripHtmlValidation } = require(path.join(dir, "strip.js"));
const N = parseInt(process.argv[3] || "600", 10);

const curated = [
  "",
  "plain text, no tags",
  "We <3 you. Pro+ is $9/month",
  "We <3 the Studio plan",
  "Plans <= Pro+ is $9/month",
  "Pricing <\tPro+ is $9/month",
  "a < b and c > d",
  "x<3y",
  "<p>Pro is $9.95/month.</p><p>Free includes 2 private bundles.</p>",
  "<p>LoopSkill is free to self-host.</p><p>WiseChef runs it for you from $199/month.</p>",
  "P<br>ro<p>includes 2 private bundles</p>",
  "Pro<b>+</b> for agencies",
  "<ul><li>Free: 2 private bundles</li><li>Pro: 50 private bundles</li></ul>",
  '<p><a href="https://app.loopskill.io/pricing">Current plans</a></p>',
  "<p>Pro &amp; Free, &lt;b&gt; is literal</p>",
  "<h1>LoopSkill</h1><h2>Pro</h2><h3>Free</h3>",
  "<strong>Pro</strong> <em>is</em> <u>$9.95/month</u>",
  "<span>Pro</span>",
  "Free<br/>Pro<br />",
  '<p>See <A HREF="https://recipes.wisechef.ai">our site</A></p>',
  "<p>x</p><ul><li>Our Pro\n+ plan</li></ul>",
  "<p>x</p><ul><li>recipes.\nwisechef.ai</li></ul>",
  "<p>a</p><ul><li><p>Pro</p><p>+ plan</p></li></ul>",
  "<P>Pro</P><UL><LI>Free: 2 private bundles</LI></UL>",
  "<p><strong>Pro</strong>+ and <u>Free</u></p>",
  "<p>a</p><h2>Pro</h2><h3>+</h3>",
];

// Deterministic PRNG (mulberry32) so the fixture is reproducible.
let seed = 0x1006c1a1;
function rnd() {
  seed |= 0;
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
const atoms = [
  "<", ">", "/", "!", "?", "-", '"', "'", "=", " ", "\n", "3", "p", "b", "x",
  "Pro", "+", "ro", " for agencies", "2 private bundles", " includes ", "$9/month", "$9.95/month",
  "<p>", "</p>", "<br>", "<br/>", "<b>", "</b>", "<strong>", "</strong>", "<li>", "</li>", "<ul>", "</ul>",
  "<span>", "</span>", "<a href=\"x\">", "</a>", "<h2>", "</h2>",
  "<!--", "-->", "--!>", "<?", "<!x", "</3", "<<", ">>",
  "<textarea>", "</textarea>", "<table>", "<td>", "<script>", "</script>", "<style>", "<title>",
  "&amp;", "&lt;", "&gt;", "&nbsp;", "&#43;", "Studio plan", "Recipes ", "powers your agents",
  "<A HREF=\"x\">", "</A>", "<LI>", "</LI>", "<UL>", "<P>", "</P>", "\n", "\r\n", "<a href=\"y\">", "<u>", "</u>", "<h3>", "</h3>",
];
const fuzz = [];
for (let i = 0; i < N; i++) {
  const n = 1 + Math.floor(rnd() * 12);
  let s = "";
  for (let k = 0; k < n; k++) s += atoms[Math.floor(rnd() * atoms.length)];
  fuzz.push(s);
}
// Grammar-based fuzz: WELL-FORMED documents (the shape the gate accepts), so
// every emulated mode is exercised on realistic structure, not just rejects.
const words = [
  "Pro", "+", "Pro+", "Free", " ", "\n", "\r\n", "<3", "a < b", "x>y", "&amp;", "&lt;", "&gt;", "&nbsp;", "&#43;",
  "2 private bundles", "$9/month", "$9.95/month", "recipes.", "wisechef.ai", "Studio plan", "for agencies",
  " includes ", "-", "*", "_", "[", "]", "(", ")", "#", "LoopSkill", "API keys", "10",
];
const pick = (a) => a[Math.floor(rnd() * a.length)];
const INL = ["strong", "b", "em", "i", "u", "s", "span"];
function inline(depth, inA) {
  let out = "";
  const n = 1 + Math.floor(rnd() * 4);
  for (let k = 0; k < n; k++) {
    const r = rnd();
    if (depth > 2 || r < 0.55) out += pick(words);
    else if (r < 0.7) out += "<br>";
    else if (r < 0.85 && !inA) out += `<a href="${pick(["https://app.loopskill.io", "https://recipes.wisechef.ai", ".wisechef.ai", "9", "x"])}">${inline(depth + 1, true)}</a>`;
    else { const t = pick(INL); out += `<${t}>${inline(depth + 1, inA)}</${t}>`; }
  }
  return out;
}
function block(depth) {
  const r = rnd();
  if (r < 0.5) return `<p>${inline(0, false)}</p>`;
  if (r < 0.65) { const h = `h${1 + Math.floor(rnd() * 3)}`; return `<${h}>${inline(0, false)}</${h}>`; }
  const tag = rnd() < 0.7 ? "ul" : "ol";
  let items = "";
  const n = 1 + Math.floor(rnd() * 3);
  for (let k = 0; k < n; k++) {
    items += depth < 1 && rnd() < 0.3 ? `<li>${block(depth + 1)}${block(depth + 1)}</li>` : `<li>${inline(0, false)}</li>`;
  }
  return `<${tag}>${items}</${tag}>`;
}
function doc() {
  let d = "";
  const n = 1 + Math.floor(rnd() * 4);
  for (let k = 0; k < n; k++) d += block(0);
  return d;
}
const structured = [];
for (let i = 0; i < N; i++) structured.push(doc());
const out = [...curated, ...fuzz, ...structured].map((s) => [
  s,
  stripHtmlValidation("none", s),
  stripHtmlValidation("normal", s, true),
  stripHtmlValidation("markdown", s),
]);
process.stdout.write(JSON.stringify(out, null, 0).replace(/\],\[/g, "],\n[") + "\n");
