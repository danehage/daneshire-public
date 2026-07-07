# Intent: Scanner overhaul — from options-era screener to Daneshire funnel

Confirmed with Dane 2026-07-07 (interview-me session). This is the intent behind
issues [#48](https://github.com/danehage/daneshire/issues/48),
[#49](https://github.com/danehage/daneshire/issues/49),
[#50](https://github.com/danehage/daneshire/issues/50),
[#51](https://github.com/danehage/daneshire/issues/51).

- **Outcome:** The scanner becomes a Daneshire-setup funnel: scan the S&P 500 +
  S&P 400 (~900 disjoint names, Robinhood universe deleted), rescore so the
  ranking means "quality name temporarily knocked down" instead of "magnitude of
  recent damage" (HV-rank demoted from co-largest weight; distress signals stop
  triple-counting the same crash), and the ticker detail page gains a hybrid
  context block — instant fundamentals/drawdown/headlines, plus a Gemini-written
  "what happened to this stock" paragraph filling in ~3s later, fetched only on
  click and cached daily.
- **User:** Dane alone — specifically the version of him with 20 spare minutes
  after a scan, deciding which unknown names deserve research time.
- **Why now:** The score, universes, and HV weighting were inherited from the
  options-era v1; Dane has moved to share-based value swings (covered calls on
  exit only) and the tool never followed.
- **Success:** The top of the scan list is worth Gemini time — fewer falling
  knives ranked #1, and unknown mid-caps go from "homework you skip" to a
  30-second read, so the 900-name pond actually gets fished.
- **Constraint:** Scan cost and speed stay roughly where they are today —
  context generation is per-click (pennies/month on existing Gemini prepaid
  credits), never per-scan.
- **Out of scope:** No fundamental scoring model inside the scan; no auto-buy
  verdicts (the app surfaces, Gemini/Claude discusses, Dane triggers); no
  options/IV features beyond what exists.

## How Dane actually consumes scan results (from the interview)

1. Skims for names he knows; skips known garbage (e.g. AMC) even at high scores.
2. Then unknown names if time permits, biased toward higher-priced stocks —
   his heuristic for filtering retail fluff and for institutional follow-through
   ("once BlackRock/Vanguard finds it undervalued, they buy in big").
3. For each candidate: asks Gemini "what happened to $XYZ, why is it down?" and
   converses. The context block (#50) automates exactly this first question.

## Open questions

- Stage 1 price floor value (proposed $20–30) — Dane hasn't picked. (#48)
- Whether the "not interested" blocklist is wanted at all, and its UX. (#51)
- Whether QUICK_SCAN keeps its meme-era entries or gets re-curated. (#48)

## Resolved 2026-07-07 (later the same day)

- **Scoring design confirmed and posted to #49** (now `ready-for-agent`).
  Validated against 5 historical reference setups (3 wins, 1 loss, 1
  control. Core insight: score stabilization evidence (RSI divergence across
  swing lows — 5/5 separator — undercut deceleration, bounce holding vs
  fading), never damage itself. Old score ranked AMC above all three winners.
  Full structure, weights envelope, and test fixtures in the #49 comment.
