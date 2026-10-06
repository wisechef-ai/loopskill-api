// Regenerate tests/fixtures/striptags_3_2_0.json from the REAL striptags@3.2.0
// that Postiz runs (verified by md5 against the postiz container, 2026-10-06).
//
//   curl -sfL https://raw.githubusercontent.com/ericnorris/striptags/v3.2.0/src/striptags.js -o /tmp/striptags.js
//   node deploy/claimgate/gen_striptags_fixture.js /tmp/striptags.js > tests/fixtures/striptags_3_2_0.json
//
// claims_normalize.strip_tags(x, "join") and claimgate.strip_tags(x, 'join')
// must reproduce every output byte for byte (tests/test_claims_contract*.py).
// Add an input here whenever a tag-parsing bypass is found.
"use strict";
const striptags = require(require("path").resolve(process.argv[2]));
const corpus = [
  "",
  "plain text, no tags",
  "Pro<b>+</b> for agencies",
  "<p>P<!--'-->ro+ for agencies</p>",
  "P<!---->ro+",
  "P<!-- a -- b -->ro+",
  "P<!-- unterminated",
  "P<!-->ro+",
  "P<!--->ro+",
  'Pro<b title=">">+</b>',
  "Pro<b title='>'>+</b>",
  "Pro<b title=\"'>\">+</b>",
  "a < b and c > d",
  "x<y",
  "x< y",
  "x<\ny",
  "<<b>>Pro+",
  "<a <b>>Pro+</a>",
  "Pro<br>includes 2 private bundles",
  "P<b>ro</b><br>includes 2 private bundles",
  "P<br>ro<p>includes 2 private bundles</p>",
  "<p>Pro is $9.95/month.</p><p>Free includes 2 private bundles.</p>",
  "Rec<br>ipes<p>powers your agents</p>",
  "unterminated <b title=\"x",
  "Pro<b title=\"a'b\">+</b>",
  "<!DOCTYPE html>Pro+",
  "<?xml version=\"1.0\"?>Pro+",
  "Pro&#43; and <i>&amp;</i>",
  "emoji 😀<b>Pro</b>+",
  "<ul><li>Free: 2 private bundles</li><li>Pro: 50 private bundles</li></ul>",
  "<p>LoopSkill costs, unlike WiseChef, $199/month.</p>",
  "tab<\tb>x",
  "<b\n>Pro</b>+",
];
process.stdout.write(JSON.stringify(corpus.map((s) => [s, striptags(s)]), null, 1) + "\n");
