"""Build the tier-1 US macro-event calendar for the macro-events Tier-1 study.

Aggregates raw FMP `economics-calendar` API dumps (JSON arrays, one file per
date-range chunk) into `data/tier1_calendar.csv` — one row per release
*instance* (a release datetime carries many FMP sub-series rows; they collapse
to one instance here).

Point-in-time rules applied (see REPORT.md "Data provenance"):
- CPI/NFP: FMP carries duplicate rows for the same release under two naming
  families; only the family stamped at the true BLS release time
  (08:30 ET = 12:30/13:30 UTC) is kept. The duplicate family carries bogus
  early-morning UTC timestamps and is dropped by the time-of-day whitelist.
- The 2024-08-21 "Non Farm Payrolls (Mar)" row is the QCEW annual benchmark
  revision, not the monthly Employment Situation print -> dropped by the same
  whitelist.
- FOMC decisions on 2020-03-03 and 2020-03-15 were unscheduled emergency
  intermeeting actions -> excluded (not knowable ex ante). All other rows are
  scheduled releases.

Run:  python3 build_calendar.py --src '/path/to/mcp-FMP-economics-*.txt'
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re

import structlog

log = structlog.get_logger(module="macro_events.build_calendar")

OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "tier1_calendar.csv")

# (event_type, importance, name pattern, allowed UTC times-of-day or None=any)
SPECS = [
    ("CPI", "high", re.compile(r"^(Core )?(CPI|Inflation Rate)\b", re.I), {"12:30", "13:30"}),
    ("NFP", "high", re.compile(r"^Non ?Farm Payrolls( Private)?\s*\(", re.I), {"12:30", "13:30"}),
    ("FOMC_DECISION", "high", re.compile(r"^Fed Interest Rate Decision$", re.I), None),
    ("FOMC_MINUTES", "medium", re.compile(r"^FOMC Minutes$", re.I), None),
    ("PCE", "medium", re.compile(r"^(Core )?PCE Price Index", re.I), None),
]

UNSCHEDULED_FOMC = {"2020-03-03", "2020-03-15"}  # emergency intermeeting actions

# Independent spot checks: (event_type, expected instance datetime UTC).
# CPI rows cross-checked against BLS news-release archive URLs (release date in
# the URL, 08:30 ET stated in the release): cpi_02122025 / cpi_03122025 /
# cpi_04102025(schedule note) / cpi_02132026. FOMC rows are the publicly known
# 14:00 ET statement times for the 2017-02-01 / 2022-06-15 meetings.
SPOT_CHECKS = [
    ("CPI", "2025-02-12 13:30:00"),
    ("CPI", "2025-03-12 12:30:00"),
    ("CPI", "2025-04-10 12:30:00"),
    ("CPI", "2026-02-13 13:30:00"),
    ("CPI", "2026-06-10 12:30:00"),
    ("NFP", "2017-02-03 13:30:00"),
    ("FOMC_DECISION", "2017-02-01 19:00:00"),
    ("FOMC_DECISION", "2022-06-15 18:00:00"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="glob of raw FMP economics-calendar dumps")
    args = ap.parse_args()

    instances: dict[tuple[str, str], set[str]] = {}
    files = sorted(glob.glob(args.src))
    if not files:
        raise SystemExit(f"no source files match {args.src!r}")
    for fp in files:
        with open(fp) as f:
            try:
                data = json.load(f)
            except ValueError:
                log.warning("skip_unparseable_file", path=fp)
                continue
        if not isinstance(data, list):
            continue
        for e in data:
            if e.get("country") != "US":
                continue
            name = (e.get("event") or "").strip()
            dt = e.get("date") or ""
            for etype, _imp, pat, allowed_tod in SPECS:
                if not pat.search(name):
                    continue
                if allowed_tod is not None and dt[11:16] not in allowed_tod:
                    break  # duplicate naming family w/ bogus timestamp, or benchmark revision
                if etype == "FOMC_DECISION" and dt[:10] in UNSCHEDULED_FOMC:
                    break
                instances.setdefault((etype, dt), set()).add(name)
                break

    for etype, dt in SPOT_CHECKS:
        if (etype, dt) not in instances:
            raise SystemExit(f"SPOT CHECK FAILED: {etype} @ {dt} missing from aggregate")
    log.info("spot_checks_passed", n=len(SPOT_CHECKS))

    imp = {etype: i for etype, i, _p, _t in SPECS}
    rows = sorted(instances.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            ["event_type", "scheduled_at_utc", "importance", "source", "source_series_names"]
        )
        for (etype, dt), names in rows:
            w.writerow([etype, dt, imp[etype], "fmp_economics_calendar", "; ".join(sorted(names))])
    log.info("calendar_written", path=OUT_PATH, n_instances=len(rows))


if __name__ == "__main__":
    main()
