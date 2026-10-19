#!/usr/bin/env python3
"""Summarise a watch log produced by watch.sh.

Usage:
    python3 analyze.py watch.log [--iface re0] [--csv samples.csv]

Prints when DRV_OACTIVE first stuck on, the packet rates around it, and how
often the flag was set before and after. Optionally writes one row per sample
to a CSV.
"""
import argparse
import csv
import datetime as dt
import re
import sys

TS_FMT = "%a %b %d %H:%M:%S UTC %Y"


def parse(path, iface):
    samples, cur = [], None
    row_re = re.compile(
        r"^(\S+?)\*?\s+\d+\s+<Link#\d+>\s+\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
    )
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("==="):
                if cur:
                    samples.append(cur)
                cur = {"ts": dt.datetime.strptime(line.strip("= "), TS_FMT),
                       "oactive": False, "status": None, "ifs": {}}
                continue
            if cur is None:
                continue
            if line.startswith(f"{iface}: flags="):
                cur["oactive"] = "DRV_OACTIVE" in line
            elif line.strip().startswith("status:"):
                cur["status"] = line.split(":", 1)[1].strip()
            else:
                m = row_re.match(line)
                if m:
                    name = m.group(1)
                    ipk, ierr, idrop, opk, oerr, coll = map(int, m.groups()[1:])
                    cur["ifs"][name] = dict(ipkts=ipk, ierrs=ierr, idrop=idrop,
                                            opkts=opk, oerrs=oerr, coll=coll)
    if cur:
        samples.append(cur)
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--iface", default="re0", help="LAN interface being watched")
    ap.add_argument("--csv", help="write per-sample rows here")
    args = ap.parse_args()

    s = parse(args.log, args.iface)
    if len(s) < 2:
        sys.exit("not enough samples")

    names = sorted({n for x in s for n in x["ifs"]})
    rows = []
    for prev, cur in zip(s, s[1:]):
        secs = (cur["ts"] - prev["ts"]).total_seconds() or 1
        row = {"time_utc": cur["ts"].strftime("%Y-%m-%d %H:%M:%S"),
               "interval_s": int(secs),
               f"{args.iface}_oactive": int(cur["oactive"]),
               f"{args.iface}_status": cur["status"]}
        for n in names:
            a, b = prev["ifs"].get(n), cur["ifs"].get(n)
            if a and b:
                row[f"{n}_in_pps"] = round((b["ipkts"] - a["ipkts"]) / secs)
                row[f"{n}_out_pps"] = round((b["opkts"] - a["opkts"]) / secs)
                row[f"{n}_errors"] = b["ierrs"] + b["idrop"] + b["oerrs"] + b["coll"]
        rows.append(row)

    span = s[-1]["ts"] - s[0]["ts"]
    gaps = [r for r in rows if r["interval_s"] > 25]
    print(f"Samples: {len(s)}  span: {span}  gaps >25s: {len(gaps)}")

    # First sample after which OACTIVE never clears again
    stuck_idx = None
    for i in range(len(rows)):
        if all(r[f"{args.iface}_oactive"] for r in rows[i:]):
            stuck_idx = i
            break

    if stuck_idx is None:
        print(f"{args.iface}: DRV_OACTIVE never stuck on")
    else:
        before, after = rows[:stuck_idx], rows[stuck_idx:]
        b_on = sum(r[f"{args.iface}_oactive"] for r in before)
        print(f"{args.iface}: DRV_OACTIVE stuck on from {after[0]['time_utc']} UTC")
        if before:
            print(f"  before: {b_on}/{len(before)} samples set "
                  f"({100 * b_on / len(before):.0f}%)")
        print(f"  after:  {len(after)}/{len(after)} samples set (100%)")
        print("  around the transition:")
        wan = next((n for n in names if n.startswith("pppoe")), None)
        for r in rows[max(0, stuck_idx - 3):stuck_idx + 4]:
            wan_pps = r.get(f"{wan}_in_pps", "?") if wan else "?"
            print(f"    {r['time_utc']}  WAN in {wan_pps:>7} pps  "
                  f"OACTIVE={'yes' if r[f'{args.iface}_oactive'] else 'no'}")

    total_err = max((r.get(f"{n}_errors", 0) for r in rows for n in names), default=0)
    print(f"Max cumulative error count on any interface: {total_err}")

    if args.csv:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
