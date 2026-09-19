#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Derived-interval support: record loading, tier derivation, and the artefact
# an interval-construction rule emits.
#
# This lives with the victims rather than in the probe library or in a reports
# directory. The library cannot host it — interval construction is workload
# knowledge, and a rule that is true of one topology is false of the next. A
# reports directory would make it an artefact of one run, when in fact it is a
# component that produced a dataset and has to be versioned alongside the
# victim whose events it consumes.
import hashlib
import json
import os
import sys

TIER_EXACT = "exact"
TIER_BOUNDED = "bounded"
TIER_UNJOINABLE = "unjoinable"

# The domain fields that decide whether two records share a time base.
DOMAIN_KEYS = ("domain.id", "domain.counter_identity", "domain.frequency_hz")


def load_record(path):
    """Header dict, column list, rows. The record is primary and is never
    modified by a rule; a rule reads it and writes a separate artefact."""
    hdr, rows, cols = {}, [], None
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("# "):
                k, _, v = line[2:].partition("=")
                hdr[k] = v
            elif cols is None and line.startswith("context_id"):
                cols = line.split(",")
            elif cols:
                rows.append(line.split(","))
    if cols is None:
        raise ValueError("%s: no column header; not an event record" % path)
    return hdr, cols, rows


def content_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def derive_tier(headers):
    """The tier follows from the records' domain fields. Nobody sets it.

    One domain: intervals are exact. Different domains with a measured origin
    offset: bounded — and no record produced so far carries one, so that path
    is unreachable here rather than unimplemented. Different domains with no
    measured offset: unjoinable, and no interval may be emitted at all."""
    first = headers[0]
    same = all(h[k] == first[k] for h in headers for k in DOMAIN_KEYS)
    evidence = {k: sorted({h[k] for h in headers}) for k in DOMAIN_KEYS}
    if same:
        return TIER_EXACT, {k: first[k] for k in DOMAIN_KEYS}
    origins = {h.get("domain.origin_identity", "none") for h in headers}
    if origins == {"none"}:
        return TIER_UNJOINABLE, evidence
    return TIER_BOUNDED, evidence


def pct(sorted_v, p):
    if not sorted_v:
        return float("nan")
    r = int(p * len(sorted_v) + 0.5)
    return sorted_v[max(0, min(len(sorted_v) - 1, r - 1))]


def instrument_block(headers, intervals_ns):
    """The inputs' null intervals, and this result's instrument-to-signal
    ratio.

    The ratio is reported against the null interval, which is the part of the
    instrument's cost that is measurable from one record. It is a LOWER BOUND:
    per-switch cost — counter state saved and restored when the workload's own
    scheduling moves the context — is invisible to a calibration of
    back-to-back marks and is not in this figure. Separating that needs a
    counters-on/off pair of runs, which is a campaign act, not a rule's."""
    nulls = [int(h["instrument.null_interval_p50_ns"]) for h in headers]
    p50 = pct(intervals_ns, 0.50) if intervals_ns else float("nan")
    worst_null = max(nulls) if nulls else 0
    return {
        "null_interval_p50_ns": nulls,
        "null_interval_p99_ns": [int(h["instrument.null_interval_p99_ns"])
                                 for h in headers],
        "time_source": sorted({h["instrument.time_source"] for h in headers}),
        "time_source_cost_ns": sorted({int(h["instrument.time_source_cost_ns"])
                                       for h in headers}),
        "counters_enabled": sorted({h["counters.enabled"] for h in headers}),
        "counter_set": sorted({h.get("counters.set", "none") for h in headers}),
        "in_line_instrument_to_signal": (round(worst_null / p50, 4)
                                         if p50 and p50 == p50 and p50 > 0
                                         else None),
        "in_line_instrument_to_signal_note":
            "lower bound: the null interval cannot see per-switch counter "
            "cost; a counters-on/off pair is required for the full figure",
    }


def artefact(rule_name, rule_version, parameters, input_paths, headers,
             tier, tier_evidence, pairing, intervals_ns, metric):
    """The derived-interval artefact (one per rule application)."""
    ns = sorted(intervals_ns)
    return {
        "artefact": "mcib.derived_intervals",
        "artefact_version": 1,
        "metric": metric,
        "rule": {
            "name": rule_name,
            "version": rule_version,
            "parameters": parameters,
        },
        "inputs": [
            {"path": p, "content_hash": content_hash(p),
             "context": h["context"], "events": int(h["run.events"])}
            for p, h in zip(input_paths, headers)
        ],
        "tier": tier,
        "tier_evidence": tier_evidence,
        "pairing": pairing,
        "instrument": instrument_block(headers, ns),
        "intervals": {
            "unit": "ns",
            "count": len(ns),
            "p50": pct(ns, .50), "p99": pct(ns, .99),
            "p999": pct(ns, .999), "p9999": pct(ns, .9999),
            "min": ns[0] if ns else None,
            "max": ns[-1] if ns else None,
        },
    }


def write_artefact(art, path, values=None):
    """The artefact beside the values it summarises. Refuses to overwrite: a
    derived result is written once, like the record it came from."""
    for p in (path, path.replace(".json", ".values.csv")) if values else (path,):
        if os.path.exists(p):
            raise SystemExit("refusing to overwrite existing artefact: %s" % p)
    with open(path, "w") as f:
        json.dump(art, f, indent=2, sort_keys=True)
        f.write("\n")
    if values is not None:
        vp = path.replace(".json", ".values.csv")
        with open(vp, "w") as f:
            f.write("interval_ns\n")
            for v in values:
                f.write("%d\n" % v)
    return path


def summarise(art, stream=sys.stdout):
    i, p = art["intervals"], art["pairing"]
    print("rule %s v%s  tier=%s  metric=%s"
          % (art["rule"]["name"], art["rule"]["version"], art["tier"],
             art["metric"]), file=stream)
    print("  inputs: %s" % ", ".join("%s(%s)" % (x["context"], x["events"])
                                     for x in art["inputs"]), file=stream)
    print("  pairing: matched=%d unmatched=%d rejected=%d"
          % (p["matched"], p["unmatched"]["count"], p["rejected"]["count"]),
          file=stream)
    for reason, n in sorted(p["unmatched"]["by_reason"].items()):
        print("    unmatched %-28s %d" % (reason, n), file=stream)
    for reason, n in sorted(p["rejected"]["by_clause"].items()):
        print("    rejected  %-28s %d" % (reason, n), file=stream)
    if i["count"]:
        print("  intervals us: p50=%.2f p99=%.2f p99.9=%.2f p99.99=%.2f max=%.2f"
              % (i["p50"] / 1000, i["p99"] / 1000, i["p999"] / 1000,
                 i["p9999"] / 1000, i["max"] / 1000), file=stream)
    print("  in-line instrument/signal: %s"
          % art["instrument"]["in_line_instrument_to_signal"], file=stream)
