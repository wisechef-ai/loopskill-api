You are writing a test set for a marketing claims checker for the product LoopSkill (an AI-agent skill marketplace at app.loopskill.io).

GROUND TRUTH (the ONLY true pricing / plan facts):
- Public hosted tiers: Free costs $0. Pro costs $9.95 per month. Billing is MONTHLY only; there is NO annual / yearly plan and no other billing period.
- Private bundles per tier: Free 2, Pro 50. Public bundles are unlimited on every tier, including Free.
- Active API keys per tier: Free 1, Pro 10.
- Founding Member: $49 one-time payment, Pro for life, capped at 100 seats.
- Self-hosting is $0 (open source, MPL-2.0), no account, no caps.
- On-demand option for organisations: contact us, no published price.
- RETIRED (now false to claim as current): a "Pro+" tier (it was $100/month, 200 bundles), "cookbooks" (old name for bundles; e.g. "Pro gets 1 cookbook"), the product name "Recipes" / recipes.wisechef.ai, a "Team" or "Enterprise" tier with a price.
- WiseChef is a DIFFERENT product (managed service, from $199/month). Mentioning WiseChef's own price in a WiseChef-only sentence is true.

TASK: write exactly 200 marketing snippets (tweets / LinkedIn lines / landing copy) as a real copywriter or an LLM content pipeline would write them. Vary tone, currency formats ($, USD, €, "9.95 dollars"), period phrasing (/mo, per month, a month, monthly, per user per month, annually, /yr), word order (tier before or after the price), lists, emojis, line breaks, and markdown.

- 100 FALSE snippets, each containing exactly one claim that contradicts the ground truth in one of these categories (spread evenly):
  wrong_price (any LoopSkill / tier price other than $0, $9.95/month, $49 one-time), annual_or_other_period (any yearly/weekly/etc. LoopSkill price, or "billed annually" / "annual plan" / "save with yearly billing"), wrong_private_bundle_limit, wrong_api_key_limit, unlimited_private_or_keys ("unlimited private bundles", "unlimited API keys"), retired_tier (Pro+ / Pro Plus as a current offer), retired_vocab (cookbooks, Recipes brand), founding_wrong (Founding price not $49 or not one-time, or seats not 100).
- 100 TRUE snippets that state ground-truth facts correctly (including ones that mention prices, limits, Founding, self-hosting, WiseChef's own price, loss/savings figures like "downtime costs teams $8,000 a day" that are not LoopSkill prices, and ordinary non-pricing marketing lines).

Do NOT write deliberately obfuscated text (no homoglyphs, no spelled-out numbers like "nine ninety-five", no zero-width characters). Natural copy only.

OUTPUT: only JSON Lines, one object per line, no prose, no code fences:
{"label": "false", "category": "<one category above>", "text": "<sentence>"}
{"label": "true", "category": "true", "text": "<sentence>"}

HELD-OUT BATCH RULES (important):
- Avoid the obvious phrasings ("Pro is $X/month", "Free includes N private bundles"). Write the way real posts do: 1-3 sentences, threads, hooks, questions, comparisons, customer quotes, FAQ answers, bullet lists, CTA lines, casual tone, typos-free.
- Put the false claim mid-sentence or in the second sentence, not up front.
- Also include false claims the categories allow in less common shapes: tier names after numbers, limits phrased as "up to", "room for", "max", "cap", comparisons ("5x more bundles than Free"), and implied periods ("pay once a year").
