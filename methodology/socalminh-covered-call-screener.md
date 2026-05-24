# SoCalMinh Covered-Call Screener — Methodology Extraction

> **Pipeline:** traditional (Whisper-base STT + RapidOCR + targeted vision_analyze
> calls). No multimodal LLM reasoning. This document is the "control" branch in a
> diff against the Gemini-3.1-Pro branch on the same inputs.

## Source

| Field        | Value                                              |
| ------------ | -------------------------------------------------- |
| URL          | https://www.instagram.com/reel/DYe45Cxi5ju/        |
| Author       | Minh Nguyen — `@socalminh` ("Wealth building & Covered Calls") |
| Co-presenter | His son, on-camera name **"Enzo"** (transcript)    |
| Upload date  | 2026-05-18                                         |
| Duration     | 75.49 s                                            |
| Series tag   | "Week 2" (text overlay top-left, frames 1–3)       |
| Hook overlay | "#1 tool used to turn my 12yr old into a millionaire" |

## Verbatim transcript (Whisper base, light cleanup)

> **Minh:** So, Enzo, before we start doing covered calls — buying stocks to
> make you a millionaire — what's the number-one tool we use?
>
> **Enzo:** Number-one tool we use is a stock screener.
>
> **Minh:** What's a stock screener? Why is it important?
>
> **Enzo:** A stock screener is, basically, a metal detector which searches
> stocks that you do want, and saves you time on finding stocks.
>
> **Minh:** Correct. And what's our number-one tool — website to use?
>
> **Enzo:** **\<Parture / "Bartchart"\>** *(unintelligible to base-model
> Whisper; see "Open questions")*.
>
> **Minh:** Cool. With our stock screener, with our four filters —
>
> **Enzo:** The first one is mid-cap, because we want **2 billion to 10
> billion**, which is the sweet spot. Then we want 10% — we want to make 10%
> every month on premium. That's our second filter. Third filter is 21 to 36
> days; we want to rent it out every month so we can make 10%. And buy-to-sell
> ratio — if everyone wants it to sell, then…
>
> **Minh:** …then it tanks the stock.
>
> **Enzo:** Then it tanks the stock.
>
> **Minh:** Which is bad for us. So we want a sell-to-buy ratio — put-to-call
> — within **0.2 to 0.7**. So if it's over 0.7, we don't want to buy that
> stock. And that's how we make sure that the stock finds us. It makes it easy
> for us to make 10% a month.

(Source: `/tmp/reels/analysis/whisper/DYe45Cxi5ju.txt`. Whisper rendered
"point-two-point-7" as "two point two to point7"; the IG caption confirms the
range is 0.2–0.7, not 2.2–0.7.)

## Visual walkthrough

The reel is **100 % talking-head**. Two subjects (father + son) in a
living-room with a wood-paneled accent wall, ceiling-fan light, designer
vinyl figure, sneaker shelf, and a Snoopy print. **No screener UI, broker
window, browser tab, ticker, chart, watermark, or URL is ever shown
on-screen.** The only visible text is:

| Overlay                                              | When                |
| ---------------------------------------------------- | ------------------- |
| "Week 2"                                             | frames 1–3 (~0–4 s) |
| "#1 tool used to turn my 12yr old into a millionaire"| frames 1–3          |
| Auto-style burned-in subtitles tracking the dialog   | frames 4–37         |

OCR (RapidOCR-ONNX) over all 38 keyframes confirms: every recognised line is
a re-statement of the spoken caption. The single "weird" OCR token —
`UERATUTIO` on f_016 — is a misread of stylised hand-lettering on the Snoopy
poster ("KEEP LOOKING UP …"). Vision-tool spot-checks on frames
1, 12, 16, 20, 24, and 32 corroborate: no broker UI, no URL, no logo, no
ticker symbol is on screen at any point.

**Implication:** the methodology must be lifted from audio + caption only.
The "tool" / "website" name is verbal-only and was not pronounced clearly
enough for `whisper-base` to lock on. Higher-tier ASR (whisper-large or
faster-whisper-medium) is required to disambiguate it; see Open Questions.

## Codified screener spec

Format: each rule as a deterministic predicate we could plug into a Python
screener (e.g. against yfinance + a P/C-ratio source).

| # | Rule (machine-readable)                                | Threshold  | Source                                            | Confidence |
| - | ------------------------------------------------------ | ---------- | ------------------------------------------------- | ---------- |
| 1 | `2_000_000_000 <= market_cap <= 10_000_000_000`        | mid-cap    | Audio @ 00:28–00:36; IG caption filter #1         | **STATED** |
| 2 | `expected_monthly_premium_pct >= 0.10` (i.e. 10 % of underlying / month) | "premium potential" | Audio @ 00:36–00:42; IG caption filter #2 ("strong monthly premium income") | **STATED (audio); INFERRED numeric 10 %** — IG caption omits the 10 %, only audio gives it |
| 3 | `21 <= dte <= 36` (calendar days to expiration)        | DTE band   | Audio @ 00:42–00:50; IG caption filter #3         | **STATED** |
| 4 | `0.2 <= put_call_ratio <= 0.7`                         | sentiment  | Audio @ 00:57–01:10; IG caption filter #4         | **STATED** |
| 4b | `if put_call_ratio > 0.7: reject`                     | hard skip  | Audio @ 01:07–01:10 explicit                      | **STATED** |

Rationale Minh gives, paraphrased:

* **(1) Mid-cap "sweet spot."** Big enough for liquid options chains, small
  enough to still pay meaningful premium — the verbal phrase is "sweet spot,"
  no further justification.
* **(2) 10 %/month premium target.** The yield bar a candidate must clear
  before they'll consider writing a call against it. *Caveat:* it's
  unspecified whether this is annualised premium ÷ 12, or the literal
  near-the-money monthly call premium ÷ stock price. Best guess
  (consistent with rule 3): monthly call premium / share price ≥ 10 % at the
  21–36-DTE strike they intend to write.
* **(3) 21–36 DTE.** Echoes the standard "tastytrade 45-DTE then manage at
  21" logic compressed into one window — they enter ≤ 36 DTE and let it run
  (or let it expire) by 0 DTE, opening fresh each month.
* **(4) P/C ratio 0.2–0.7.** Filters out names with lopsided put demand
  (high P/C ⇒ bearish flow / hedging pressure ⇒ shares can crater, which
  caps a covered-call writer's upside while preserving the downside). The
  0.2 floor is unjustified verbally — possibly an exclusion of "no put
  market exists / illiquid options" candidates. **INFERRED.**

## Tickers / examples mentioned

**None.** No specific ticker symbol is uttered, captioned, or shown
on-screen anywhere in the 75 seconds.

## Open questions / ambiguities

1. **Screener tool name.** Whisper-base transcribed the word as "Parture."
   Most plausible candidates, in descending order:
   * **Barchart** (`barchart.com/options/covered-calls`) — known
     covered-call-educator default; "Bar-chart" → "Bart-chart" → "Parture"
     is a reasonable base-model error. **Best guess.**
   * **Optionstrat / OptionsPlay / Market Chameleon** — possible but less
     phonetically consistent.
   * Action: re-run with `whisper-large-v3` or `faster-whisper-medium`
     before committing the implementation. Until then, treat the tool as
     **unknown** and implement the four numeric filters provider-agnostic.

2. **"10 % premium" semantics.** Monthly premium ÷ stock price? Annualised?
   At-the-money or out-of-the-money strike? Author does not say. Default
   for a Hermes-Quant implementation: ATM (or first OTM) call's bid /
   underlying close, at the *expiry chosen by rule 3*, ≥ 10 %.

3. **P/C ratio source.** Stock-level P/C from option volume? Open
   interest? Today vs. 30-day? Not specified. Barchart, if that is the
   tool, defaults to total-volume P/C, daily.

4. **Lower P/C bound (0.2).** Why exclude very low P/C? Possibly
   "ensure liquid put market exists" or "avoid pure-call-frenzy meme
   names." Minh doesn't justify it on camera.

5. **Liquidity filters.** No explicit volume / open-interest / spread
   thresholds are mentioned, despite being essential for a covered-call
   workflow. The Hermes-Quant port should add a sanity layer
   (`avg_option_volume_30d ≥ X`, `bid_ask_pct ≤ Y`) that this video
   simply doesn't cover.

6. **Risk overlays / position sizing.** Out of scope for the reel — the
   video stops at "the right stocks find us." Not a methodology gap;
   logged for completeness.

## Files produced by this run

* `/tmp/reels/analysis/whisper/DYe45Cxi5ju.txt` — raw Whisper output
* `/tmp/reels/analysis/transcript_traditional.txt` — same, kept under the
  expected name
* `/tmp/reels/analysis/ocr_per_frame.txt` — RapidOCR per-frame dump (38
  frames; mostly burned-in subtitles)
* this report

## Top-3 codified rules (machine-actionable summary)

1. `2e9 ≤ market_cap ≤ 1e10`  (mid-cap window)
2. `21 ≤ dte ≤ 36`            (option-expiry window)
3. `0.2 ≤ put_call_ratio ≤ 0.7` (sentiment band; reject if > 0.7)

(plus rule 2-from-audio: `monthly_call_premium / spot ≥ 0.10` at the chosen
DTE.)
