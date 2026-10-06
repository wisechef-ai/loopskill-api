// Regenerate tests/fixtures/postiz_pipeline.json from Postiz's REAL converter.
//
// The modules come from the running postiz container (parse5 6.0.1,
// striptags 3.2.0, tslib) plus its compiled strip.html.validation.js; see
// deploy/claimgate/README.md "Regenerating the pipeline fixture".
//
//   node deploy/claimgate/gen_postiz_pipeline_fixture.js <dir-with-strip.js-and-node_modules> [N]
//
// Output: [[input, published], ...] where `published` is
// stripHtmlValidation("none", input): parse5 parse + serialize, then
// striptags, then Postiz's entity decode. This is the text a plain-text
// platform receives. Property tested in tests/test_claims_contract.py: for
// every input the gate does NOT flag as unsupported markup, the gate's "join"
// reading equals `published` (after the same whitespace normalisation).
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
];
const fuzz = [];
for (let i = 0; i < N; i++) {
  const n = 1 + Math.floor(rnd() * 12);
  let s = "";
  for (let k = 0; k < n; k++) s += atoms[Math.floor(rnd() * atoms.length)];
  fuzz.push(s);
}
const out = [...curated, ...fuzz].map((s) => [s, stripHtmlValidation("none", s)]);
process.stdout.write(JSON.stringify(out, null, 0).replace(/\],\[/g, "],\n[") + "\n");
