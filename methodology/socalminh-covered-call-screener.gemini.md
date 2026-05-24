# SoCalMinh — Covered-Call Stock Screener Methodology

**Source format:** Methodology report — Gemini multimodal extraction
**Author analyzed:** Minh Nguyen (@socalminh)
**Reel ID:** DYe45Cxi5ju (Instagram)
**Video duration:** ~75 seconds
**Co-presenter:** Minh's son, "Enzo" (~12 yrs old per the on-screen overlay)

---

## Source

| Field | Value |
| --- | --- |
| Reel URL slug | `DYe45Cxi5ju` |
| Local video file | `/tmp/reels/DYe45Cxi5ju.mp4` (8.36 MB, 75 s) |
| Caption file | `/tmp/reels/DYe45Cxi5ju.description` |
| Hashtags | `#CoveredCalls #FinancialEducation #StockMarket` |
| Disclaimer (caption) | "Educational purposes only. Not financial advice." |
| Class plug (caption) | Live class every Saturday at 9:00 AM PST |

**On-screen overlays (visual):**
- Top banner (red): `Week 2`
- Center banner (white): `#1 tool used to turn my 12yr old into a millionaire`

**Extraction model:** `gemini-3-pro-preview` (Google AI Studio, multimodal — full MP4 sent as one upload).
The model attempted ID was `gemini-3-pro-preview`; this is Google's Gemini 3 Pro preview generation. The
task asked for "Gemini 3.1 Pro" — that exact ID is not yet exposed in the public Files API; the closest
currently-available preview is `gemini-3-pro-preview` and that is what was used. Total wall time for the
multimodal call: **34.5 s**. No fallback to 2.5 Pro was needed.

---

## Verbatim transcript (with timestamps)

> **[0:00] Dad (Minh):** So Enzo, before we start doing cover[ed] calls to buy stocks to make you a millionaire, what's the number one tool we use?
> **[0:06] Son (Enzo):** Number one tool we use is a stock screener.
> **[0:09] Dad:** What's a stock screener and why is it important?
> **[0:11] Enzo:** Stock screener is a metal detector basically which searches stocks that you do want and saves you time on finding stocks.
> **[0:20] Dad:** Correct. And what's our number one tool / website to use it?
> **[0:22] Enzo:** Barchart.
> **[0:24] Dad:** Cool. Uh — with our stock screener, with our four filters. The first one is…
> **[0:28] Enzo:** Mid cap, because we want 2 billion to 10 billion, which is the sweet spot.
> **[0:36] Enzo:** Then we want 10%. We want to make 10% every month on premium.
> **[0:40] Dad:** That's our second filter. Third filter…
> **[0:42] Enzo:** Third filter is 21 to 36 days. We want to rent it out every month so we can make 10%.
> **[0:50] Enzo:** And buy-to-sell ratio. If everyone wants to sell, then it tanks the stock.
> **[0:58] Dad:** Then it tanks the stock.
> **[0:59] Enzo:** Then it tanks the stock.
> **[1:00] Dad:** Which is bad for us.
> **[1:01] Enzo:** Which is bad for us.
> **[1:01] Dad:** So we want a sell-to-buy ratio — put-to-call — within point two to point seven. So if it's over point seven, we don't want to buy that stock.
> **[1:09] Enzo:** Yes.
> **[1:10] Dad:** And that's how we make sure that the stock finds us. It makes it easy for us to make 10% a month. Alright?

---

## Visual walkthrough

This is a **dialogue-only reel**. There is **no screen recording, no broker UI, and no screener UI** at any
point in the 75 seconds.

- **Setting (entire video, 0:00–1:15):** Static, front-facing camera. Father and son seated on a couch
  speaking to camera.
- **Background props:** Ceiling fan, wood-paneled accent wall, floating shelves displaying a red art toy
  (resembling a KAWS figure), a single sneaker on display, framed Snoopy poster.
- **Text overlays:** `Week 2` (red banner, top) and `#1 tool used to turn my 12yr old into a millionaire`
  (white banner, center).
- **Auto-captions:** Standard auto-generated white-on-black subtitle band along the bottom throughout.
  The captions contain a transcription error at ~1:04 — they render the father's "point two to point seven"
  as "two point two to point seven", which is mechanically wrong.

**Implication for downstream codification:** Since the screener UI is never shown, every numeric threshold
below comes from spoken audio only. Any column-name mapping into Barchart's actual screener fields requires
a separate pass against Barchart's docs.

---

## Codified screener spec

The four filters, exactly as the speakers describe them:

| # | Field (as spoken) | Likely Barchart field | Operator | Value | Source | Confidence |
| - | --- | --- | --- | --- | --- | --- |
| 1 | "Mid cap" | Market Capitalization | between | **$2 B – $10 B** | spoken @ 0:28 | High |
| 2 | "10% every month on premium" | (option premium yield, monthly) | **≥ 10%** | per month | spoken @ 0:36, reiterated @ 1:13 | High — but the *measurement basis* is not specified (premium / strike? premium / share price? annualised vs monthly?) |
| 3 | "21 to 36 days" | Days to Expiration (DTE) | between | **21 – 36 days** | spoken @ 0:42 | High |
| 4 | "Put-to-call … within point two to point seven" | Put/Call Volume Ratio (or Open-Interest variant) | between | **0.20 – 0.70** | spoken @ 1:01 | High — explicitly bounded; "over 0.7" is rejected |

**Three rules I'd put first into a screener config:**

1. `market_cap BETWEEN 2_000_000_000 AND 10_000_000_000`
2. `dte BETWEEN 21 AND 36`
3. `put_call_ratio BETWEEN 0.20 AND 0.70`

The 10%-monthly-premium rule needs a denominator decision before it can be codified — see *Confidence notes*.

---

## Tool / broker identification

- **Identified:** **Barchart** (`barchart.com`).
- **Spoken:** Confidence **HIGH** — Enzo says "Barchart" at 0:22 in direct response to "what's our number
  one tool / website to use it?"
- **Visual:** Confidence **N/A** — no UI, logo, URL bar, column headers, or screenshot of any kind appears
  in the video. Identification is **audio-only**.

For implementers: Barchart's covered-call screener (`barchart.com/options/covered-calls`) supports market-cap,
DTE, and put/call-ratio filters natively, so the four spoken filters are directly expressible there.

---

## Tickers and concrete examples

**None.** No ticker symbol is spoken, written, or shown on screen at any point in the reel.

This is a meta-discussion about *the screener and its filter values*, not a stock-picking demonstration.

---

## Caption-vs-video deltas

| Caption claim | What the video actually says | Delta |
| --- | --- | --- |
| "Premium potential" (qualitative) | "We want to make 10% every month on premium" | **Video adds a numeric threshold (≥10%/mo)** that the caption omits. |
| "21 to 36 days" | "21 to 36 days" | Match. |
| "$2 B–$10 B mid-cap" | "2 billion to 10 billion" | Match. |
| "P/C ratio 0.2–0.7" | "point two to point seven" | Match (audio); the auto-caption layer in the reel itself mistakenly displays "two point two to point seven" — that is an Instagram-side OCR/ASR artefact, **not** a methodology change. |
| (caption silent) | Speaker explicitly states they're using **Barchart** | **Video adds the tool name.** |
| (caption silent) | Speaker frames the goal as **"make it easy for us to make 10% a month"** (1:10) | Video adds the explicit return target. |

---

## Confidence notes

| Section | Confidence | Notes |
| --- | --- | --- |
| Transcript | High | Both speakers enunciate clearly; no background noise. |
| Visual walkthrough | High | Static shot, no UI, no fast cuts — nothing to misread. |
| Screener spec — values | High | All four numeric ranges are stated explicitly. |
| Screener spec — *units* of filter #2 | **Low** | "10% every month on premium" does not specify whether that's premium-as-percent-of-share-price, premium-as-percent-of-strike, simple monthly yield, or annualised. A naive reading of Barchart's "Premium" column gives a yield-style figure but the basis is platform-dependent. **Implementers must resolve this before treating it as a hard filter** — the safest move is to set it as a soft / ranking filter and tune empirically against historical data. |
| Tool identification | High (audio) / N/A (visual) | Spoken only. Verify against Barchart's screener UI separately. |
| Tickers / examples | 100% | None present. |
| Methodology completeness | Medium | The reel says "four filters" and lists four. There is no mention of liquidity floors, IV-rank requirements, earnings-blackout exclusion, dividend-stripping risk, or position-sizing — all of which a production covered-call workflow typically needs. The video is an introductory rule-of-thumb, **not** a full screener. |

---

## Suggested follow-up (out of scope of this extraction)

1. Pull Barchart's actual covered-call screener column names and map filters #1–#4 onto specific URL parameters.
2. Decide the denominator for filter #2 (premium / strike vs premium / share price) and document the choice.
3. Add liquidity & IV-rank guards before backtesting.
4. Independently verify the 10%-monthly-yield claim against historical mid-cap covered-call data — that is
   the kind of return that, if real and repeatable, would imply ~213 % annualised, which warrants strong
   skepticism on a risk-adjusted basis.
