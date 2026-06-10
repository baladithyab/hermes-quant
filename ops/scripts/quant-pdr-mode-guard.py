#!/usr/bin/env python3
"""quant-pdr-mode-guard.py — self-healing guard for quant.pdr.mode.

Codeseys' autonomous trading mode kept silently reverting to the
`advise`/`hitl` default (the `quant:` block being dropped/empty in
~/.hermes/config.yaml), which silences the autonomous PDR tick. This
no_agent watchdog re-asserts the desired mode if it ever drifts.

Behavior (classic watchdog / silence-by-default):
  - Reads quant.pdr.mode from ~/.hermes/config.yaml.
  - If it already equals DESIRED_MODE  -> print NOTHING, exit 0 (silent).
  - If it is a VALID non-autonomous mode (hitl/advise) -> treat as an INTENTIONAL
    operator downgrade (a safety pause) and LEAVE IT ALONE: print nothing, exit 0.
    Re-arming autonomous adds capital risk and must require positive intent; a
    downgrade removes risk and is the operator's call to make (Codex P1, 2026-06-10).
  - If it drifted to MISSING/EMPTY/INVALID (the documented clobber — the `quant:`
    block being dropped/emptied) -> re-assert DESIRED_MODE via an atomic,
    full-dict-preserving write (never rebuilds from a schema, so no other config
    keys are touched), append a forensic line to a drift log, and print ONE alert
    line so the operator learns a revert occurred.
  - If config.yaml is missing or unparseable -> print an error line, exit 1
    (the cron's error path alerts; a broken guard must never fail silently).

Why this is safe:
  - It only ever WRITES the single key quant.pdr.mode (and creates the
    quant/pdr parent dicts if absent). The watchlist, cadence, and every
    unrelated top-level key are read in and written back verbatim.
  - It does NOT touch .env, the risk gate, the sizing ladder, the
    kill-switch, or any trading state. Mode persistence only.
  - Atomic rename write (tmp + os.replace) — no torn config.yaml.

Deploy: wired as a no_agent Hermes cron. Empty stdout == silent (no
operator message); non-empty stdout == the alert is delivered verbatim.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

DESIRED_MODE = "autonomous"
VALID_MODES = {"advise", "hitl", "autonomous"}
CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"
DRIFT_LOG = Path.home() / ".hermes" / "quant" / "pdr-mode-drift.jsonl"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("quant-pdr-mode-guard: pyyaml is required", file=sys.stderr)
        return 1

    if not CONFIG_PATH.exists():
        print(f"🚨 quant-pdr-mode-guard: {CONFIG_PATH} does not exist", file=sys.stderr)
        return 1

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text) or {}
    except Exception as e:  # noqa: BLE001
        print(f"🚨 quant-pdr-mode-guard: failed to parse config.yaml: {e}", file=sys.stderr)
        return 1

    if not isinstance(cfg, dict):
        print("🚨 quant-pdr-mode-guard: config.yaml root is not a mapping", file=sys.stderr)
        return 1

    quant = cfg.get("quant")
    pdr = quant.get("pdr") if isinstance(quant, dict) else None
    current = pdr.get("mode") if isinstance(pdr, dict) else None

    if current == DESIRED_MODE:
        # No drift — stay silent (empty stdout => no operator message).
        return 0

    # SAFETY ASYMMETRY (Codex P1 review, 2026-06-10): re-arming autonomous ADDS
    # capital risk and must require positive intent; an operator downgrade to a
    # valid non-autonomous mode REMOVES risk and MUST be respected. This guard
    # exists for the *clobber* case — the `quant:` block being dropped/emptied so
    # `mode` goes missing/None — NOT to override a deliberate safety pause. So we
    # ONLY re-assert when the mode is missing/empty/invalid. A valid explicit
    # `hitl`/`advise` is treated as an intentional operator downgrade and left
    # alone (stay silent, do not re-arm — that pause is the operator's call).
    if isinstance(current, str) and current.strip().lower() in (VALID_MODES - {DESIRED_MODE}):
        # Intentional downgrade to a valid non-autonomous mode — respect it.
        return 0

    # Otherwise the mode is missing / empty / garbage — the documented clobber.
    # Re-assert the desired mode, preserving every other key.
    quant = cfg.setdefault("quant", {})
    if not isinstance(quant, dict):
        quant = cfg["quant"] = {}
    pdr = quant.setdefault("pdr", {})
    if not isinstance(pdr, dict):
        pdr = quant["pdr"] = {}
    pdr["mode"] = DESIRED_MODE

    # Atomic, full-dict-preserving write (no schema rebuild — keeps watchlist,
    # cadence, and all unrelated keys verbatim).
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".pdrguard.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"🚨 quant-pdr-mode-guard: failed to write config.yaml: {e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return 1

    # Forensic drift log (append-only) so we can eventually catch the culprit.
    try:
        DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
        import json
        with open(DRIFT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "asof": _now_iso(),
                "drifted_to": current,
                "reasserted": DESIRED_MODE,
            }) + "\n")
    except Exception:  # noqa: BLE001
        pass  # logging failure must not block the re-assert

    drifted = repr(current) if current is not None else "<missing>"
    print(
        f"⚙️ quant-pdr-mode-guard: PDR mode had drifted to {drifted} — "
        f"re-asserted '{DESIRED_MODE}'. Autonomous trading restored. "
        f"(drift logged to {DRIFT_LOG})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
