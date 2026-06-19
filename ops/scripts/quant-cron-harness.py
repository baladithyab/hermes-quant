#!/usr/bin/env python3
"""quant-cron-harness.py — observe + drive the hermes-quant cron suite locally.

WHY THIS EXISTS
---------------
The live hermes-quant system is driven by ~26 cron jobs registered in
``~/.hermes/cron/jobs.json`` (the Hermes cron daemon, NOT the OS crontab). Each
job fires a script in ``~/.hermes/scripts/`` at a cron schedule. To understand
"what does a full trading day look like / what does hermes-agent expect", you
otherwise have to wait for real wall-clock cron fires spread across the day.

This harness reads the SAME ``jobs.json`` and lets you either:

  * ``--list``                 : show every quant job, its schedule, next fire, and script.
  * ``--run <name|id|all>``    : run one job (or all) ON DEMAND, right now, and
                                 capture stdout/stderr/exit-code/duration so you
                                 can see exactly what each run produces and expects.
  * ``--daemon``               : run 24/7 as a faithful scheduler — evaluate each
                                 job's cron expr each minute and fire it at the
                                 same minute the real Hermes cron would.

SAFETY POSTURE (money-software)
-------------------------------
This is a READ + OBSERVE tool by default. It NEVER edits jobs.json, .env, or live
state. Crucially it defaults to a SAFE OBSERVE posture for each job:

  * scripts that take ``--dry-run`` are run WITH ``--dry-run``;
  * the ``--armed`` wrapper scripts (``*-armed.sh``) are NOT fired in observe mode
    — they would place paper trades. They are only invoked when you pass
    ``--armed`` to THIS harness (an explicit opt-in mirroring the wrappers' own
    ``--armed`` gate), and even then daemon mode warns loudly first.
  * ``--json`` is added where the script supports it (machine-observable output).

So ``--run all`` with no ``--armed`` shows you the whole day's behavior without
firing a single paper trade. Add ``--armed`` only when you deliberately want the
fire path (e.g. against a freshly-reset paper book).

This script does NOT depend on croniter (not installed in the venv); it ships a
small standard 5-field cron matcher covering the expr forms these jobs use
(``*``, ``a,b``, ``a-b``, ``*/n``, ``a-b/n``, day-of-week ``1-5``).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
JOBS_JSON = HERMES_HOME / "cron" / "jobs.json"
SCRIPTS_DIR = HERMES_HOME / "scripts"
VENV_PY = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python3"
HARNESS_LOG = HERMES_HOME / "quant" / "cron-harness.jsonl"


# --------------------------------------------------------------------------- #
# Cron expression matcher (standard 5-field: min hour dom month dow)
# --------------------------------------------------------------------------- #
def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the set of matching integers in [lo, hi]."""
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(base)
        v = start
        while v <= end:
            if lo <= v <= hi:
                values.add(v)
            v += step
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    """True if the 5-field cron ``expr`` fires at minute ``dt`` (second ignored).

    Day-of-week: cron uses 0-6 = Sun-Sat; Python weekday() is 0-6 = Mon-Sun, so
    we map. Both 0 and 7 mean Sunday in cron. When BOTH dom and dow are
    restricted (not ``*``), standard cron fires if EITHER matches (the OR rule);
    here the quant jobs never restrict both, so a simple AND-with-OR-fallback is
    used.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {len(fields)!r}: {expr!r}")
    minute, hour, dom, month, dow = fields
    cron_dow = (dt.weekday() + 1) % 7  # Mon=0..Sun=6  ->  Sun=0..Sat=6
    min_ok = dt.minute in _parse_field(minute, 0, 59)
    hour_ok = dt.hour in _parse_field(hour, 0, 23)
    month_ok = dt.month in _parse_field(month, 1, 12)
    dom_set = _parse_field(dom, 1, 31)
    dow_set = {0 if d == 7 else d for d in _parse_field(dow, 0, 7)}
    dom_restricted = dom.strip() != "*"
    dow_restricted = dow.strip() != "*"
    dom_ok = dt.day in dom_set
    dow_ok = cron_dow in dow_set
    if dom_restricted and dow_restricted:
        day_ok = dom_ok or dow_ok  # standard cron OR rule
    else:
        day_ok = dom_ok and dow_ok
    return min_ok and hour_ok and month_ok and day_ok


def next_fire(expr: str, after: datetime, horizon_minutes: int = 60 * 24 * 8) -> datetime | None:
    """Return the next minute at/after ``after`` that ``expr`` fires, within horizon."""
    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(horizon_minutes):
        if cron_matches(expr, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


# --------------------------------------------------------------------------- #
# Job model
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    name: str
    expr: str
    enabled: bool
    script: str | None
    skill: str | None
    prompt_head: str
    raw: dict = field(default_factory=dict)

    @property
    def is_armed_wrapper(self) -> bool:
        return bool(self.script) and self.script.endswith("-armed.sh")

    @property
    def is_prompt_driven(self) -> bool:
        """No script -> the job is an agent/skill/prompt run (interim briefs)."""
        return not self.script


def _schedule_expr(job: dict) -> str:
    sched = job.get("schedule")
    if isinstance(sched, dict):
        return sched.get("expr") or sched.get("display") or ""
    if isinstance(sched, str):
        return sched
    return job.get("schedule_display") or ""


def load_quant_jobs(only_enabled: bool = False) -> list[Job]:
    if not JOBS_JSON.exists():
        raise SystemExit(f"jobs.json not found at {JOBS_JSON}")
    data = json.loads(JOBS_JSON.read_text(encoding="utf-8"))
    jobs = data if isinstance(data, list) else data.get("jobs", data)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out: list[Job] = []
    for j in jobs:
        blob = json.dumps(j).lower()
        if "quant" not in blob and "catalyst" not in blob and "calibrator" not in blob:
            continue
        if only_enabled and not j.get("enabled"):
            continue
        out.append(
            Job(
                id=str(j.get("id", "")),
                name=str(j.get("name", "?")),
                expr=_schedule_expr(j),
                enabled=bool(j.get("enabled")),
                script=j.get("script"),
                skill=j.get("skill"),
                prompt_head=(j.get("prompt") or "")[:200].replace("\n", " "),
                raw=j,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Running a job
# --------------------------------------------------------------------------- #
def build_command(job: Job, *, armed: bool) -> tuple[list[str] | None, str]:
    """Return (argv, note). argv is None when the job is skipped in observe mode.

    Observe posture: add --dry-run / --json where supported; never fire an armed
    wrapper unless ``armed`` is set.
    """
    if job.is_prompt_driven:
        return None, "prompt/agent-driven (no script) — needs the hermes-agent runtime; skipped by the harness"

    # An armed (-armed.sh) wrapper in OBSERVE mode is skipped regardless of whether the
    # file is present — the skip is a posture decision, not a file lookup. Checking this
    # BEFORE the existence check keeps the observe-skip note stable when the deployed
    # AND repo copies are both absent (CI / a fresh HERMES_HOME with no scripts/ dir),
    # where the harness would otherwise mis-report "script not found".
    if job.is_armed_wrapper and not armed:
        return None, "armed wrapper — observe mode skips it (pass --armed to fire the paper path)"

    script_path = SCRIPTS_DIR / job.script
    if not script_path.exists():
        # Fall back to the repo's ops/scripts copy if the deployed one is absent.
        repo_copy = Path(__file__).resolve().parent / job.script
        if repo_copy.exists():
            script_path = repo_copy
        else:
            return None, f"script not found ({job.script})"

    if job.is_armed_wrapper:
        # armed=True reaches here only when the wrapper file actually exists.
        return ["bash", str(script_path)], "ARMED wrapper — will fire the paper path"

    # Plain .py / .sh script.
    body = script_path.read_text(encoding="utf-8", errors="replace")
    if script_path.suffix == ".py":
        argv = [str(VENV_PY), str(script_path)]
    else:
        argv = ["bash", str(script_path)]
    note_bits = []
    if not armed and "--dry-run" in body:
        argv.append("--dry-run")
        note_bits.append("--dry-run")
    if "--json" in body:
        argv.append("--json")
        note_bits.append("--json")
    return argv, ("observe: " + " ".join(note_bits) if note_bits else "run")


def run_job(job: Job, *, armed: bool, timeout: int = 300) -> dict:
    argv, note = build_command(job, armed=armed)
    stamp = datetime.now(timezone.utc).isoformat()
    if argv is None:
        rec = {"asof": stamp, "job": job.name, "skipped": True, "note": note}
        _log(rec)
        print(f"  [skip] {job.name}: {note}")
        return rec
    print(f"  [run ] {job.name}: {note}\n         $ {' '.join(argv)}")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=str(SCRIPTS_DIR)
        )
        dur = time.monotonic() - t0
        rec = {
            "asof": stamp,
            "job": job.name,
            "argv": argv,
            "exit_code": proc.returncode,
            "duration_s": round(dur, 2),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
            "note": note,
        }
        status = "OK" if proc.returncode == 0 else f"EXIT {proc.returncode}"
        print(f"         -> {status} in {dur:.1f}s")
        if proc.stdout.strip():
            print(_indent(proc.stdout.strip()[-1200:]))
        if proc.returncode != 0 and proc.stderr.strip():
            print("         stderr:")
            print(_indent(proc.stderr.strip()[-800:]))
    except subprocess.TimeoutExpired:
        dur = time.monotonic() - t0
        rec = {"asof": stamp, "job": job.name, "argv": argv, "timeout": True, "duration_s": round(dur, 2), "note": note}
        print(f"         -> TIMEOUT after {dur:.0f}s")
    _log(rec)
    return rec


def _indent(text: str, prefix: str = "         | ") -> str:
    return "\n".join(prefix + ln for ln in text.splitlines())


def _log(rec: dict) -> None:
    try:
        HARNESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HARNESS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    jobs = load_quant_jobs(only_enabled=args.enabled_only)
    now = datetime.now(timezone.utc)
    print(f"{len(jobs)} quant cron jobs in {JOBS_JSON}:\n")
    print(f"  {'en':3} {'schedule':20} {'next fire (UTC)':17} {'name':40} script")
    for job in sorted(jobs, key=lambda j: j.expr):
        en = "ON " if job.enabled else "off"
        try:
            nf = next_fire(job.expr, now)
            nf_s = nf.strftime("%m-%d %H:%M") if nf else "—"
        except Exception as exc:  # noqa: BLE001
            nf_s = f"?({exc})"[:16]
        scr = job.script or (f"skill:{job.skill}" if job.skill else "(prompt-driven)")
        print(f"  {en:3} {job.expr:20} {nf_s:17} {job.name[:40]:40} {scr}")
    return 0


def _select_jobs(args) -> list[Job]:
    jobs = load_quant_jobs(only_enabled=args.enabled_only)
    target = args.run
    if target == "all":
        return jobs
    matches = [j for j in jobs if j.name == target or j.id == target]
    if not matches:
        matches = [j for j in jobs if target.lower() in j.name.lower()]
    if not matches:
        raise SystemExit(f"no quant job matching {target!r} (try --list)")
    return matches


def cmd_run(args) -> int:
    jobs = _select_jobs(args)
    if args.armed:
        print("⚠️  --armed: armed wrappers WILL fire the paper path (real paper fills).\n")
    print(f"Running {len(jobs)} job(s) (armed={args.armed}, timeout={args.timeout}s):\n")
    results = [run_job(j, armed=args.armed, timeout=args.timeout) for j in jobs]
    ok = sum(1 for r in results if r.get("exit_code") == 0)
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = sum(1 for r in results if r.get("exit_code") not in (0, None))
    print(f"\nSummary: {ok} ok, {failed} failed, {skipped} skipped of {len(results)}.")
    print(f"Full transcript appended to {HARNESS_LOG}")
    return 1 if failed else 0


def cmd_daemon(args) -> int:
    jobs = load_quant_jobs(only_enabled=True)
    print(f"quant-cron-harness daemon: watching {len(jobs)} enabled quant jobs.")
    print(f"  armed={args.armed}  (observe mode skips -armed wrappers)" if not args.armed
          else "  ⚠️  ARMED: -armed wrappers WILL fire paper trades at their scheduled minute.")
    print("  Ctrl-C to stop.\n")
    last_fired_minute: dict[str, str] = {}
    try:
        while True:
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            minute_key = now.strftime("%Y-%m-%dT%H:%M")
            for job in jobs:
                try:
                    if not cron_matches(job.expr, now):
                        continue
                except Exception:  # noqa: BLE001 - a malformed expr never crashes the daemon
                    continue
                if last_fired_minute.get(job.id) == minute_key:
                    continue  # already fired this minute
                last_fired_minute[job.id] = minute_key
                print(f"[{minute_key}] fire {job.name}")
                run_job(job, armed=args.armed, timeout=args.timeout)
            # Sleep to the next minute boundary.
            time.sleep(max(1, 60 - datetime.now(timezone.utc).second))
    except KeyboardInterrupt:
        print("\ndaemon stopped.")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list quant jobs + next fire times")
    g.add_argument("--run", metavar="NAME|ID|all", help="run a job (or 'all') on demand now")
    g.add_argument("--daemon", action="store_true", help="run 24/7 as a faithful scheduler")
    ap.add_argument("--armed", action="store_true",
                    help="fire the armed/paper path (default: safe observe — dry-run, skip -armed wrappers)")
    ap.add_argument("--enabled-only", action="store_true", help="only jobs marked enabled in jobs.json")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-job wall-clock cap seconds (default 600; network/LLM jobs "
                         "like universe-scan + watchlist-evolve need minutes, not seconds)")
    args = ap.parse_args(argv)
    if args.list:
        return cmd_list(args)
    if args.run:
        return cmd_run(args)
    if args.daemon:
        return cmd_daemon(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
