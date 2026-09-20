#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Tier 2 attribution: what the kernel event stream says about one run.
#
# Counters name a hardware channel — cache, TLB, branch predictor. This names
# a kernel software one: a lock, a softirq, an inter-processor interrupt, a
# preemption. It is the question most metrics land in, because most of them
# come back with flat counters, and on the metrics whose intervals span two
# contexts it is the only tier that can say anything at all.
#
# WHAT IS NOT HERE, AND WHY THAT MATTERS MOST. There is no clock-anchor
# reconstruction. An earlier approach to this problem stamped the endpoints
# of a run and then rebuilt each iteration's window as an anchor plus the
# running sum of the measured intervals. That sum omits the gaps between
# iterations, so the reconstructed timeline drifts away from the real one —
# far enough that a reconstructed span of 0.686-0.697 s stood against a 2-3 s
# wall-clock envelope, and every event placed into it was placed by
# arithmetic rather than by observation.
#
# The probe records a kernel timestamp on every mark, in the same clock the
# collector selects when it opens its events. So an interval has real bounds
# in that clock, an event has a real timestamp in it, and the event either
# falls between the bounds or it does not. No anchor, no cumulative sum, no
# estimated offset. That the two instruments do share one base is not assumed
# here: it was measured on the reference board over 600,000 joins, where
# every interval contained exactly one scheduler switch and not one switch
# preceded the mark that opened its interval.
#
# The join still tolerates a small negative skew rather than asserting strict
# ordering, because the fast timestamp accessor these events are stamped by
# is documented as not guaranteed monotonic across a timekeeper update. That
# window is nanoseconds wide and rare; asserting it away would be asserting
# something the documentation does not promise.
#
# GUARDS, each from an observation:
#
#   - a minimum absolute count, because an enrichment computed from a handful
#     of events is not a finding: a five-event enrichment was once labelled a
#     likely cause while 195 of 200 outlier windows held no event at all;
#   - coverage, reported with every ratio, because an enrichment that
#     explains 2.5% of the outlier population is not an explanation of the
#     outlier population;
#   - events that occur in every interval by construction are excluded,
#     because a scheduler switch on a context-switching metric is what the
#     metric IS, and enriching on it measures the workload rather than the
#     interference.
#
# WHAT THIS TIER CANNOT SAY. It cannot see a stall that emits no event. A
# non-preemptible section that simply runs long produces nothing to join
# against. Where the counters are flat, displacement is flat and no event is
# enriched, the honest result is that neither instrument resolves the path —
# stated as both instruments' null results, not as an absence of a problem.
import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcib_derive import content_hash, load_record, pct     # noqa: E402
from rules import apply_declared                           # noqa: E402
from thresholds import Thresholds                          # noqa: E402

CAPTURE_KIND = "mcib.trace_capture"
NO_SIGNAL = "no signal"
OK = "ok"


def load_capture(path):
    """A capture and its self-description.

    Everything the analysis needs to decide whether this capture may be used
    is in its own header — the source, the clock, the event set, the ring
    size, the window and the drop count. That is the point of the format: a
    capture that cannot say what it is cannot be checked, and a capture that
    has to be checked against a separate note about how it was taken will
    eventually be checked against the wrong one."""
    header, rows = {}, []
    with open(path) as f:
        cols = None
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("# "):
                k, _, v = line[2:].partition("=")
                header[k] = v
            elif cols is None and line.startswith("t_kernel_ns"):
                cols = line.split(",")
            elif cols:
                parts = line.split(",")
                rows.append((int(parts[0]), parts[2]))
    if header.get("mcib_capture") != CAPTURE_KIND:
        raise SystemExit("%s is not a trace capture" % path)
    return header, rows


def _interval_bounds(art, paths):
    """Per-interval [T0, T1] in the kernel clock, from the artefact's own
    rule applied to the records the artefact names."""
    _, intervals_ns, endpoints = apply_declared(art, paths,
                                                with_endpoints=True)
    kern = []
    for p in paths:
        _, cols, rows = load_record(p)
        i = cols.index("t_kernel_ns")
        kern.append([int(r[i]) for r in rows])
    bounds = []
    for (o_s, o_r, c_s, c_r) in endpoints:
        bounds.append((kern[o_s][o_r], kern[c_s][c_r]))
    return bounds, intervals_ns


def _resolve_inputs(art, artefact_path, root=None):
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(artefact_path)))
    paths, problems = [], []
    for spec in art["inputs"]:
        p = os.path.join(root, spec["path"])
        if not os.path.exists(p):
            problems.append("input named by the artefact is missing: %s"
                            % spec["path"])
            continue
        if content_hash(p) != spec["content_hash"]:
            problems.append("input %s has changed since the artefact was "
                            "written" % spec["path"])
        paths.append(p)
    return paths, problems


def trace_evidence(artefact_path, capture_path, thresholds=None, root=None,
                   collection_cost=None):
    """Tier-2 evidence for one artefact and the capture covering its run.

    Returns a plain structure: no verdict, no recommendation, no formatted
    text, and every figure carrying the counts it came from."""
    th = thresholds or Thresholds()
    art = json.load(open(artefact_path))
    header, events = load_capture(capture_path)
    metric = art["metric"]
    quantile = th.outlier_quantile(metric)

    out = {
        "artefact_path": artefact_path,
        "capture_path": capture_path,
        "metric": metric,
        "capture": {
            "source": header.get("source"),
            "clock": header.get("clock"),
            "clock_selected_at_open": header.get("clock_selected_at_open"),
            "events_requested": (header.get("events") or "").split(),
            "ring_bytes_per_event": header.get("ring_bytes_per_event"),
            "dropped": int(header.get("dropped", "0") or 0),
            "samples": int(header.get("samples", "0") or 0),
        },
        "refusals": [],
        "events": {},
        "join": None,
        "outlier_quantile": quantile,
        "collection_cost": collection_cost,
        "thresholds": th.declared(),
    }

    # ---- refusals, before any arithmetic -----------------------------------

    dropped = out["capture"]["dropped"]
    if dropped:
        out["refusals"].append(
            "capture reports %d dropped events; an enrichment computed over a "
            "hole is not an enrichment, and the capture is refused rather "
            "than used" % dropped)
        return out

    paths, problems = _resolve_inputs(art, artefact_path, root)
    out["refusals"].extend(problems)
    if problems:
        return out

    if art["tier"] not in ("exact", "bounded"):
        out["refusals"].append(
            "tier %s: an interval with no established time base has no "
            "latency to attribute" % art["tier"])
        return out

    # The join rests entirely on the two instruments sharing one clock. If
    # they do not, nothing below is meaningful, so it is checked rather than
    # assumed.
    record_clocks = set()
    for p in paths:
        h, _, _ = load_record(p)
        record_clocks.add(h.get("domain.kernel_clock"))
    cap_clock = header.get("clock")
    if record_clocks != {cap_clock}:
        out["refusals"].append(
            "the capture is stamped in %s and the records in %s; an event "
            "cannot be placed inside an interval measured on another clock"
            % (cap_clock, ", ".join(sorted(str(c) for c in record_clocks))))
        return out

    bounds, intervals_ns = _interval_bounds(art, paths)
    if not bounds:
        out["refusals"].append("the artefact carries no intervals")
        return out

    # The capture must cover the measured window. A capture that started late
    # or stopped early explains only the part it saw.
    run_begin, run_end = min(b[0] for b in bounds), max(b[1] for b in bounds)
    cap_begin = int(header.get("collection.begin_kernel_ns") or 0)
    cap_end = int(header.get("collection.end_kernel_ns") or 0)
    covered = cap_begin <= run_begin and cap_end >= run_end
    if not covered:
        out["refusals"].append(
            "the capture covers [%d, %d] and the measured intervals span "
            "[%d, %d]; the collection window does not contain the measured "
            "window" % (cap_begin, cap_end, run_begin, run_end))
        return out

    # ---- the split ---------------------------------------------------------

    lat = sorted(intervals_ns)
    outlier_threshold = pct(lat, quantile)
    nominal_threshold = (pct(lat, th.get("median_quantile"))
                         * th.get("nominal_band_factor"))

    is_outlier = [v > outlier_threshold for v in intervals_ns]
    is_nominal = [v <= nominal_threshold for v in intervals_ns]
    n_out = sum(is_outlier)
    n_nom = sum(is_nominal)

    # ---- the join ----------------------------------------------------------
    #
    # Intervals are ordered by their opening mark and searched by it. An event
    # is placed in the latest interval that opened at or before it and has not
    # yet closed. Events matching more than one interval are counted, because
    # overlapping intervals mean the rule that built them produced windows
    # that are not a partition and a reader should know that before reading an
    # enrichment over them.

    order = sorted(range(len(bounds)), key=lambda i: bounds[i][0])
    starts = [bounds[i][0] for i in order]
    longest = max(b[1] - b[0] for b in bounds)

    per_event_out = {}
    per_event_nom = {}
    covered_out = {}
    covered_any = {}
    outside = 0
    multi = 0
    negative_skew = 0

    for ts, name in events:
        j = bisect.bisect_right(starts, ts) - 1
        hit = None
        hits = 0
        k = j
        while k >= 0 and starts[k] >= ts - longest:
            idx = order[k]
            if bounds[idx][0] <= ts <= bounds[idx][1]:
                hits += 1
                if hit is None:
                    hit = idx
            k -= 1
        if hit is None:
            outside += 1
            continue
        if hits > 1:
            multi += 1
        if is_outlier[hit]:
            per_event_out[name] = per_event_out.get(name, 0) + 1
            covered_out.setdefault(name, set()).add(hit)
        elif is_nominal[hit]:
            per_event_nom[name] = per_event_nom.get(name, 0) + 1
        covered_any.setdefault(name, set()).add(hit)

    outside_fraction = outside / len(events) if events else 0.0
    out["join"] = {
        "intervals": len(bounds),
        "n_outlier": n_out,
        "n_nominal": n_nom,
        "outlier_threshold_ns": outlier_threshold,
        "nominal_threshold_ns": nominal_threshold,
        "capture_events": len(events),
        "events_in_no_interval": outside,
        "events_in_no_interval_fraction": outside_fraction,
        "events_in_more_than_one_interval": multi,
        "negative_skew_events": negative_skew,
        "outside_fraction_maximum": th.get(
            "trace_outside_window_fraction_max"),
        "method": ("each event is placed by its own timestamp into an "
                   "interval whose bounds are the probe's own kernel "
                   "readings; no anchor, no cumulative sum, no estimated "
                   "offset"),
    }
    if outside_fraction > th.get("trace_outside_window_fraction_max"):
        out["refusals"].append(
            "%.4f of the capture falls inside no measured interval, above "
            "the maximum %.4f; the collection window and the measured window "
            "disagree" % (outside_fraction,
                          th.get("trace_outside_window_fraction_max")))
        return out

    # ---- enrichment, with the guards ---------------------------------------

    min_out = th.get("trace_min_outlier_events")
    min_total = th.get("trace_min_total_events")
    coverage_floor = th.get("trace_coverage_floor")
    self_excl = th.get("trace_self_exclusion_presence")

    names = sorted(set(per_event_out) | set(per_event_nom) | set(covered_any))
    for name in names:
        o_count = per_event_out.get(name, 0)
        n_count = per_event_nom.get(name, 0)
        per_out = o_count / n_out if n_out else None
        per_nom = n_count / n_nom if n_nom else None
        presence_all = len(covered_any.get(name, ())) / len(bounds)
        coverage = (len(covered_out.get(name, ())) / n_out) if n_out else None

        e = {
            "outlier_occurrences": o_count,
            "nominal_occurrences": n_count,
            "total_occurrences": o_count + n_count,
            "per_outlier_interval": per_out,
            "per_nominal_interval": per_nom,
            "n_outlier_intervals": n_out,
            "n_nominal_intervals": n_nom,
            "coverage_of_outliers": coverage,
            "intervals_containing_it": len(covered_any.get(name, ())),
            "presence_across_all_intervals": presence_all,
            "minimum_outlier_occurrences": min_out,
            "minimum_total_occurrences": min_total,
            "coverage_floor": coverage_floor,
            "enrichment": None,
            "signal": NO_SIGNAL,
            "no_signal_reason": None,
            "self_excluded": False,
        }

        if presence_all >= self_excl:
            e["self_excluded"] = True
            e["no_signal_reason"] = (
                "present in %.4f of all intervals, at or above the "
                "self-exclusion presence %.4f: this event is part of what "
                "the metric measures, not of what interferes with it"
                % (presence_all, self_excl))
        elif o_count < min_out:
            e["no_signal_reason"] = (
                "%d occurrences in outlier intervals, below the minimum %d"
                % (o_count, min_out))
        elif o_count + n_count < min_total:
            e["no_signal_reason"] = (
                "%d occurrences in total, below the minimum %d"
                % (o_count + n_count, min_total))
        elif per_nom in (None, 0):
            e["no_signal_reason"] = (
                "the event does not occur in the nominal population: the "
                "ratio has no denominator. An absent denominator is not a "
                "large effect")
        elif per_out == 0:
            e["no_signal_reason"] = (
                "the event does not occur in the outlier population")
        else:
            e["enrichment"] = per_out / per_nom
            e["signal"] = OK
            if coverage is not None and coverage < coverage_floor:
                e["signal"] = NO_SIGNAL
                e["no_signal_reason"] = (
                    "coverage %.4f is below the floor %.4f: the enrichment is "
                    "real but explains too little of the outlier population "
                    "to be its explanation" % (coverage, coverage_floor))
        out["events"][name] = e

    # An event that was asked for and never arrived is a fact about the run,
    # not an omission, and is recorded as one.
    for name in out["capture"]["events_requested"]:
        if name not in out["events"]:
            out["events"][name] = {
                "outlier_occurrences": 0, "nominal_occurrences": 0,
                "total_occurrences": 0, "enrichment": None,
                "signal": NO_SIGNAL,
                "no_signal_reason": "requested but never occurred on the "
                                    "measured core during the run",
                "self_excluded": False, "coverage_of_outliers": 0.0,
            }
    return out


def summarise(ev, stream=sys.stdout):
    p = lambda *a: print(*a, file=stream)
    p("%s + %s  metric=%s"
      % (os.path.basename(ev["artefact_path"]),
         os.path.basename(ev["capture_path"]), ev["metric"]))
    c = ev["capture"]
    p("  capture: %s clock=%s dropped=%d samples=%d"
      % (c["source"], c["clock"], c["dropped"], c["samples"]))
    for r in ev["refusals"]:
        p("  REFUSED: %s" % r)
    j = ev["join"]
    if j:
        p("  join: %d intervals (outlier %d, nominal %d); %d events outside "
          "every interval (%.4f); %d in more than one"
          % (j["intervals"], j["n_outlier"], j["n_nominal"],
             j["events_in_no_interval"], j["events_in_no_interval_fraction"],
             j["events_in_more_than_one_interval"]))
    if ev["events"]:
        p("  %-26s %9s %9s %10s %9s  %s"
          % ("event", "out/intvl", "nom/intvl", "enrichment", "coverage",
             "note"))
        for name in sorted(ev["events"]):
            e = ev["events"][name]
            enr = ("%10.3f" % e["enrichment"]
                   if e.get("enrichment") is not None else "%10s" % NO_SIGNAL)
            po = ("%9.4f" % e["per_outlier_interval"]
                  if e.get("per_outlier_interval") is not None else "%9s" % "-")
            pn = ("%9.4f" % e["per_nominal_interval"]
                  if e.get("per_nominal_interval") is not None else "%9s" % "-")
            cov = ("%9.4f" % e["coverage_of_outliers"]
                   if e.get("coverage_of_outliers") is not None
                   else "%9s" % "-")
            note = e.get("no_signal_reason") or ""
            if e.get("self_excluded"):
                note = "SELF-EXCLUDED — " + note
            p("  %-26s %s %s %s %s  %s" % (name, po, pn, enr, cov, note))


def main(argv):
    if len(argv) < 3:
        print("usage: trace.py <derived-artefact.json> <capture> [--json]",
              file=sys.stderr)
        return 2
    ev = trace_evidence(argv[1], argv[2])
    if "--json" in argv:
        print(json.dumps(ev, indent=2, sort_keys=True))
    else:
        summarise(ev)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
