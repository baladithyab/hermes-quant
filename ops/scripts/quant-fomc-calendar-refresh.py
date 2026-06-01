#!/usr/bin/env python3
"""FOMC calendar — annual seed-refresh helper (ADR-0084 seed-staleness mitigation).

The vendored ``hermes_quant/catalyst/fomc_calendar.seed.yaml`` carries one year's
8 FOMC meeting windows + 8 communications-blackout windows. The Fed publishes the
schedule ~1+yr ahead, so the seed must be REFRESHED annually. This helper:

  * loads the current vendored seed (zero network) and reports which year it
    covers and whether it still reaches the current + next quarter (the freshness
    angle the assertion test enforces), and
  * given the next year's 8 meeting DATES (operator pastes them from
    federalreserve.gov/monetarypolicy/fomccalendars.htm — the FOMC schedule is the
    single authoritative source), DETERMINISTICALLY derives the decision instants
    (2:00 PM ET, DST-aware) and the blackout windows (the FOMC Communications-policy
    rule: SECOND Saturday preceding -> THURSDAY following each meeting), and prints
    them as a ready-to-vendor YAML block for the operator to paste + review.

The script NEVER hits the network and NEVER mutates the seed (mirrors the
graph-mine / coverage probes: it PROPOSES, the operator commits). Exit 0 always
(informational). Run annually (after the Fed posts the next year's schedule) or
when the freshness assertion warns.

Usage:
  quant-fomc-calendar-refresh.py
      -> report current seed coverage + freshness only.
  quant-fomc-calendar-refresh.py --year 2027 \\
      --meetings 2027-01-26,2027-01-27 ... (8 "start,end" pairs)
      -> also print the derived 2027 seed block for review.
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta

from hermes_quant.catalyst.calendar import load_fomc_seed

# --- US Eastern DST (no zoneinfo dependency; the rule is the published law) ----
# DST: starts 2nd Sunday of March 02:00, ends 1st Sunday of November 02:00.


def _us_eastern_is_dst(d: date) -> bool:
    """True if US/Eastern is on DST (EDT, UTC-4) for the given date (else EST, UTC-5)."""
    # second Sunday of March
    mar1 = date(d.year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)  # 2nd Sunday
    # first Sunday of November
    nov1 = date(d.year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)  # 1st Sunday
    return dst_start <= d < dst_end


def _et_to_utc(d: date, hh: int, mm: int, ss: int) -> datetime:
    """Convert an Eastern wall-clock instant to a tz-aware UTC datetime."""
    offset = 4 if _us_eastern_is_dst(d) else 5  # EDT=UTC-4, EST=UTC-5
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=UTC) + timedelta(
        hours=offset
    )


def _prev_saturday(d: date) -> date:
    """The Saturday strictly preceding d (Sat=5 in weekday())."""
    back = (d.weekday() - 5) % 7
    return d - timedelta(days=back or 7)


def _next_thursday(d: date) -> date:
    """The Thursday strictly following d (Thu=3 in weekday())."""
    fwd = (3 - d.weekday()) % 7
    return d + timedelta(days=fwd or 7)


def derive_windows(meetings: list[tuple[date, date]]) -> tuple[list[dict], list[dict]]:
    """Derive meeting decision instants + blackout windows from (day1, day2) pairs.

    * decision = 2:00 PM ET on day 2 (the statement-release instant).
    * blackout = the SECOND Saturday preceding day1 (00:00 ET) -> the THURSDAY
      following day2 (23:59:59 ET) — the FOMC Communications-policy rule.
    """
    meeting_rows: list[dict] = []
    blackout_rows: list[dict] = []
    for d1, d2 in meetings:
        decision = _et_to_utc(d2, 14, 0, 0)
        meeting_rows.append(
            {
                "meeting_dates": f"{d1.isoformat()}/{d2.isoformat()}",
                "scheduled_for": decision.isoformat().replace("+00:00", "Z"),
                "title": f"FOMC meeting ({d1.strftime('%b %-d')}-{d2.strftime('%-d')}) "
                f"— rate decision 2:00pm ET",
            }
        )
        bstart_day = _prev_saturday(d1) - timedelta(days=7)  # second Saturday preceding
        bend_day = _next_thursday(d2)
        bstart = _et_to_utc(bstart_day, 0, 0, 0)
        bend = _et_to_utc(bend_day, 23, 59, 59)
        blackout_rows.append(
            {
                "blackout_start": bstart.isoformat().replace("+00:00", "Z"),
                "blackout_end": bend.isoformat().replace("+00:00", "Z"),
                "scheduled_for": bend.isoformat().replace("+00:00", "Z"),
                "title": f"FOMC blackout ({bstart_day.strftime('%b %-d')} -> "
                f"{bend_day.strftime('%b %-d')} ET) — "
                f"{d1.strftime('%b %-d')}-{d2.strftime('%-d')} meeting",
            }
        )
    return meeting_rows, blackout_rows


def _parse_meetings_arg(raw: str) -> list[tuple[date, date]]:
    """Parse "YYYY-MM-DD,YYYY-MM-DD YYYY-MM-DD,YYYY-MM-DD ..." into (day1, day2) pairs."""
    pairs: list[tuple[date, date]] = []
    for tok in raw.replace(";", " ").split():
        d1s, _, d2s = tok.partition(",")
        pairs.append((date.fromisoformat(d1s.strip()), date.fromisoformat(d2s.strip())))
    return pairs


def _emit_yaml_block(year: int, meeting_rows: list[dict], blackout_rows: list[dict]) -> None:
    print("\n# --- review then vendor into hermes_quant/catalyst/fomc_calendar.seed.yaml ---")
    print(f"year: {year}")
    print('announced_at: "<PUBLICATION_ANCHOR>"  # the Fed press-release date for this '
          "schedule (a hard past fact, earlier than every scheduled_for)")
    print("market: US")
    print("impact: high")
    print("source: federalreserve.gov/fomccalendars")
    print("meetings:")
    for r in meeting_rows:
        print(f'  - {{meeting_dates: "{r["meeting_dates"]}", '
              f'scheduled_for: "{r["scheduled_for"]}", title: "{r["title"]}"}}')
    print("blackouts:")
    for r in blackout_rows:
        print(f'  - {{blackout_start: "{r["blackout_start"]}", '
              f'blackout_end: "{r["blackout_end"]}", '
              f'scheduled_for: "{r["scheduled_for"]}", title: "{r["title"]}"}}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, help="target year to derive a fresh seed block for")
    ap.add_argument(
        "--meetings",
        help="8 'day1,day2' pairs (space/semicolon-separated), e.g. "
        "'2027-01-26,2027-01-27 2027-03-16,2027-03-17 ...'",
    )
    args = ap.parse_args()

    # --- 1. report the current vendored seed coverage + freshness -------------
    events = load_fomc_seed()
    now = datetime.now(UTC)
    if not events:
        print("⚠️ fomc-calendar: vendored seed is EMPTY/missing — refresh it.")
    else:
        scheduled = sorted(e.scheduled_for for e in events)
        latest = scheduled[-1]
        # current + next quarter horizon (~6 months) — the freshness-test window.
        horizon = now + timedelta(days=183)
        covers = latest >= horizon
        n_meet = sum(1 for e in events if e.title.lower().startswith("fomc meeting"))
        n_black = sum(1 for e in events if e.title.lower().startswith("fomc blackout"))
        status = "✅" if covers else "⚠️ STALE"
        print(
            f"{status} fomc-calendar: {len(events)} events "
            f"({n_meet} meetings + {n_black} blackouts); "
            f"latest scheduled_for {latest.date()}; "
            f"covers current+next quarter (to {horizon.date()}): {covers}."
        )
        if not covers:
            print("   -> the seed no longer reaches the next two quarters; refresh it "
                  "with the next year's Fed-published meeting dates (use --year/--meetings).")

    # --- 2. optionally derive + print a fresh block for review ----------------
    if args.year and args.meetings:
        try:
            pairs = _parse_meetings_arg(args.meetings)
        except ValueError as e:
            print(f"⚠️ fomc-calendar: could not parse --meetings ({e}).", file=sys.stderr)
            return 0
        if len(pairs) != 8:
            print(f"⚠️ fomc-calendar: expected 8 meeting pairs, got {len(pairs)}; "
                  "the FOMC holds 8 scheduled meetings/yr.", file=sys.stderr)
        meeting_rows, blackout_rows = derive_windows(pairs)
        _emit_yaml_block(args.year, meeting_rows, blackout_rows)
    elif args.year or args.meetings:
        print("\n   (pass BOTH --year and --meetings to derive a fresh seed block.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
