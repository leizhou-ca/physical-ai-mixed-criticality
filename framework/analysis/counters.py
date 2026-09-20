#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Tier 1 attribution: what the hardware counters say about one run.
#
# The question this answers is "which hardware channel", and only that:
# cache, TLB, branch predictor, cycles. It does not answer "which kernel
# software path" — that is the trace tier — and it does not decide what
# should be done about either. Nothing here knows what a verdict is, and
# nothing here may learn: an evidence module that also judges is a module
# whose judgement cannot be changed without recomputing its evidence.
#
# WHAT MAKES THIS EXACT. Every probe mark records a counter snapshot
# alongside its timestamps, so the counters for a measured section are the
# difference between the two snapshots that bracket it, stored in the same
# rows the interval was built from. Pairing is by construction, not by
# timestamp, so nothing can drift.
#
# THE INSTRUMENT FOLLOWS THE INTERVAL'S OWNERSHIP, and the rule declares
# where it is. Two arrangements reach this module:
#
#   - the interval belongs to one context throughout, and its own two
#     endpoints carry the snapshots;
#   - the interval belongs to a context that BLOCKS AND WAKES, and that
#     context brackets its own block with a declared COUNTER SEGMENT. The
#     interval's timestamps still span two contexts; its counter delta does
#     not. Per-thread counters pause while the context is blocked, so the
#     delta is the wakeup path and nothing else.
#
# What is never done is a difference between two contexts' snapshots. Two
# per-thread counters are two streams that pause independently; two per-CPU
# counters opened by two contexts are two accumulators with different
# origins. Neither difference is a measurement of anything, and an interval
# with no delta available is counted and excluded rather than given one.
#
# GUARDS. Every guard here exists because its absence produced a wrong
# answer, not because it seemed prudent:
#
#   - a ratio is computed only above a minimum absolute count, because a
#     ratio of two tiny medians is noise amplified;
#   - a zero nominal median yields no signal and NEVER infinity, because
#     infinity is not a large effect, it is an absent denominator;
#   - both medians zero yields no signal — this is the case that produced a
#     confident cache-eviction finding, with a countermeasure attached, from
#     a baseline run that had no aggressor running at all;
#   - segments the counter backend cannot vouch for are excluded and counted,
#     per segment rather than per backend, because a backend that can detect
#     preemption but is never made to act on it declares a property it does
#     not deliver.
#
# Every figure returned carries the counts it was computed from. A ratio
# without its denominator cannot be guarded, and a guard that cannot see the
# counts cannot fire.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcib_derive import content_hash, load_record, pct     # noqa: E402
from rules import apply_declared                           # noqa: E402
from thresholds import Thresholds                          # noqa: E402

# Columns that describe the event rather than count anything. Everything else
# in a record is a counter.
STRUCTURAL_COLUMNS = ("context_id", "seq", "point_id", "token", "t_wall",
                      "t_kernel_ns", "domain_id", "flags")

# Counters the backend opens to substantiate a property it declares, rather
# than to answer the campaign's question. They are reported, because they are
# what qualification is decided from, but they are not interference channels.
QUALIFICATION_COLUMNS = ("context_switches", "cpu_migrations")

FLAG_UNQUALIFIED = 0x0004
FLAG_READ_FAILED = 0x0008

NO_SIGNAL = "no signal"
OK = "ok"


# ------------------------------------------------------------- small numerics
#
# Written out rather than taken from a numerical library because the framework
# carries no third-party dependency, and because each of these is a few lines
# whose behaviour at the edges — an empty sample, a constant column, ties —
# is exactly what the guards above are about. The constants inside them are
# the formulae's own (a variance divides by n, a rank correlation squares a
# difference); they are not thresholds and do not belong in the threshold
# inventory.

def _median(values):
    if not values:
        return None
    return pct(sorted(values), Thresholds().get("median_quantile"))


def _mean(v):
    return sum(v) / len(v)


def _pearson(xs, ys, min_stddev):
    """Linear correlation. None where either side is constant."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    if (sxx / n) ** 0.5 < min_stddev or (syy / n) ** 0.5 < min_stddev:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _ranks(v):
    """Average ranks, ties shared. Ties matter here: counter columns are
    small integers and are full of them, and a rank correlation that broke
    ties arbitrarily would invent a monotone relationship out of ordering."""
    order = sorted(range(len(v)), key=lambda i: v[i])
    out = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def _spearman(xs, ys, min_stddev):
    return _pearson(_ranks(xs), _ranks(ys), min_stddev)


# ------------------------------------------------------------------- the work

def _resolve_inputs(art, artefact_path, root=None):
    """The record paths an artefact names, verified against the hashes it
    names them with.

    An artefact's input list is the only path from a result back to the
    events it rests on. Reading a record the artefact does not name would
    break that chain silently; reading one whose content has changed since
    would break it louder. Both are refused here rather than warned about."""
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(artefact_path)))
    paths, problems = [], []
    for spec in art["inputs"]:
        p = os.path.join(root, spec["path"])
        if not os.path.exists(p):
            problems.append("input named by the artefact is missing: %s"
                            % spec["path"])
            continue
        got = content_hash(p)
        if got != spec["content_hash"]:
            problems.append(
                "input %s has changed since the artefact was written "
                "(artefact names %s, file is %s)"
                % (spec["path"], spec["content_hash"], got))
        paths.append(p)
    return paths, problems


def _segment_qualified(header, switches_delta, open_flags, close_flags):
    """Whether a counter delta covers the measured context and nothing else.

    A property of the SEGMENT, not of the backend. A backend that follows the
    thread pauses counting while the context is off-CPU, so its deltas cover
    their own execution by construction. A backend that does not follow the
    thread, but can see a context switch, is qualified exactly where no
    switch occurred inside the segment — where one did, the delta includes
    whatever else ran on that core. A backend that can do neither cannot say
    what its deltas cover and is never qualified.

    Stating this per segment is the whole point. Deciding it per backend
    lets a record claim qualification for six million events while half its
    segments structurally contain a context switch, with nothing in the
    output connecting the claim to the contradiction sitting beside it."""
    if (open_flags | close_flags) & FLAG_READ_FAILED:
        return False, "counter read failed"
    if (open_flags | close_flags) & FLAG_UNQUALIFIED:
        return False, "record marked the segment unqualified"
    follows = header.get("counters.follows_thread") == "1"
    detectable = header.get("counters.preemption_detectable") == "1"
    if follows:
        return True, None
    if detectable:
        if switches_delta is None:
            return False, ("backend declares preemption detectable but the "
                           "record carries no context-switch counter")
        if switches_delta != 0:
            return False, "context switch inside the segment"
        return True, None
    return False, ("backend follows neither the thread nor preemption; a "
                   "delta cannot be said to cover the measured context")


def _core_owned_qualification(headers, columns, rows, conditions):
    """Whether a core-owned interval's delta covers the core and nothing else.

    The context-switch test that qualifies a context-owned segment cannot be
    applied here, and applying it anyway is how this metric came to report a
    qualified fraction of zero: for a core-owned interval the switch IS the
    measurement, so a rule that disqualifies a segment containing one asks a
    question whose correct answer throws the measurement away.

    What has to hold instead is that nothing else ran on the core. Three
    facts establish it, and all three come from the run's own records and
    conditions rather than from an assumption:

      - every contributing context ran on the SAME core, and on the core the
        handle counts;
      - that core is isolated, so the scheduler placed no other runnable work
        there;
      - no context migrated during the run, so neither context left the core
        and came back to a counter that had gone on without it.

    This is a run-level verdict, not a per-segment one, because all three are
    properties of the run. Where the conditions do not record the isolated
    set, the second cannot be checked and the answer is NOT qualified: an
    unisolated core is the one case this rule exists to exclude."""
    cpus = sorted({h.get("context.cpu") for h in headers})
    handle_cpus = sorted({h.get("counters.core_cpu") for h in headers})
    evidence = {"context_cpus": cpus, "handle_cpus": handle_cpus,
                "isolated_cpus": None, "migrations": {}}
    if len(cpus) != 1 or cpus[0] is None:
        return False, ("the contributing contexts report different cores "
                       "(%s); a core-scoped delta covers one core"
                       % ", ".join(str(c) for c in cpus)), evidence
    if len(handle_cpus) != 1 or handle_cpus[0] != cpus[0]:
        return False, ("the counter handle counts core %s while the contexts "
                       "ran on core %s" % (handle_cpus, cpus[0])), evidence

    isolated = None
    for moment in ("before", "after"):
        cap = (conditions or {}).get(moment) or {}
        got = ((cap.get("isolation") or {}).get("isolated_cpus"))
        if got is not None:
            isolated = got if isolated is None else isolated
    evidence["isolated_cpus"] = isolated
    if isolated is None:
        return False, ("the conditions do not record which cores were "
                       "isolated, so it cannot be established that nothing "
                       "else ran on core %s. An unisolated core is the case "
                       "this rule exists to exclude, so the delta is not "
                       "qualified" % cpus[0]), evidence
    if int(cpus[0]) not in [int(c) for c in isolated]:
        return False, ("core %s is not in the isolated set %s; other runnable "
                       "work shared the core and is inside the delta"
                       % (cpus[0], isolated)), evidence

    total_migrations = 0
    for h, ix, rws in zip(headers, columns, rows):
        if "cpu_migrations" not in ix or len(rws) < 2:
            continue
        d = (int(rws[-1][ix["cpu_migrations"]])
             - int(rws[0][ix["cpu_migrations"]]))
        evidence["migrations"][h["context"]] = d
        total_migrations += d
    if total_migrations:
        return False, ("%d CPU migrations occurred during the run (%s); a "
                       "context that left the core and returned did not read "
                       "a counter that followed it"
                       % (total_migrations,
                          ", ".join("%s=%d" % kv for kv in
                                    sorted(evidence["migrations"].items())))), \
               evidence
    return True, None, evidence


def counter_evidence(artefact_path, thresholds=None, root=None):
    """Tier-1 evidence for one derived-interval artefact.

    Returns a plain structure. It contains no verdict, no recommendation and
    no formatted text, and every figure in it carries the counts it was
    computed from."""
    th = thresholds or Thresholds()
    art = json.load(open(artefact_path))
    metric = art["metric"]
    quantile = th.outlier_quantile(metric)

    out = {
        "artefact_path": artefact_path,
        "metric": metric,
        "rule": {"name": art["rule"]["name"],
                 "version": art["rule"]["version"]},
        "counter_state": art["instrument"]["counters_enabled"],
        "counter_set": art["instrument"]["counter_set"],
        "tier": art["tier"],
        "refusals": [],
        "counters": {},
        "displacement": None,
        "split": None,
        "qualification": None,
        "thresholds": th.declared(),
        "outlier_quantile": quantile,
    }

    paths, problems = _resolve_inputs(art, artefact_path, root)
    out["refusals"].extend(problems)
    if problems:
        return out

    if art["tier"] not in ("exact", "bounded"):
        out["refusals"].append(
            "tier %s: an interval with no established time base has no "
            "latency to attribute" % art["tier"])
        return out

    # Rebuild the pairing through the rule the artefact declares, so that the
    # events a counter delta is taken across are the same two events the
    # interval was built from, decided by the same code.
    declared_segment = art["rule"]["parameters"].get("counter_segment")
    _, intervals_ns, endpoints, counter_endpoints = apply_declared(
        art, paths, with_endpoints=True, with_counter_segment=True)

    headers, columns, rows = [], [], []
    for p in paths:
        h, c, r = load_record(p)
        headers.append(h)
        columns.append({name: i for i, name in enumerate(c)})
        rows.append(r)

    # A core-owned interval read from a core-scoped handle: one accumulator,
    # referenced by both contexts, so the difference between their snapshots
    # is one stream read twice rather than two accumulators subtracted.
    core_scoped = bool(headers) and all(
        h.get("counters.scope") == "core" for h in headers)
    core_ok, core_why, core_evidence = (True, None, None)
    if core_scoped:
        core_ok, core_why, core_evidence = _core_owned_qualification(
            headers, columns, rows, art.get("conditions"))
    out["counter_scope"] = {
        "scope": "core" if core_scoped else "context",
        "qualified": core_ok if core_scoped else None,
        "reason": core_why,
        "evidence": core_evidence,
        "rule": ("a core-owned interval is qualified when both contexts ran "
                 "on the same isolated core and nothing migrated; the "
                 "context-switch test does not apply, because for this "
                 "interval the switch is the measurement"
                 if core_scoped else
                 "the delta is taken within one context and qualified per "
                 "segment"),
    }

    counter_names = [n for n in columns[0]
                     if n not in STRUCTURAL_COLUMNS]
    question_counters = [n for n in counter_names
                         if n not in QUALIFICATION_COLUMNS]

    # ---- per-interval deltas, with qualification decided per segment -------

    samples = []            # (latency_ns, {counter: delta}, cycles_rate)
    cross_context = 0
    segment_unavailable = 0
    unqualified = 0
    unqualified_reasons = {}

    hz = int(headers[0]["domain.frequency_hz"])
    ns_per_s = th.get("ns_per_second")

    for idx, (ep, latency) in enumerate(zip(endpoints, intervals_ns)):
        os_i, o_row, cs_i, c_row = ep
        if declared_segment:
            # The delta comes from the declared segment, which is within one
            # context by construction and checked to be so by the rule.
            seg = counter_endpoints[idx]
            if seg is None:
                segment_unavailable += 1
                continue
            d_open_stream, d_open_row, d_close_stream, d_close_row = seg
        elif os_i != cs_i and not core_scoped:
            cross_context += 1
            continue
        else:
            d_open_stream, d_open_row = os_i, o_row
            d_close_stream, d_close_row = cs_i, c_row
        hdr = headers[d_open_stream]
        ix = columns[d_open_stream]
        a = rows[d_open_stream][d_open_row]
        b = rows[d_close_stream][d_close_row]
        o_flags = int(a[ix["flags"]], 16 if a[ix["flags"]].startswith("0x")
                      else 10)
        c_flags = int(b[ix["flags"]], 16 if b[ix["flags"]].startswith("0x")
                      else 10)
        sw = None
        if "context_switches" in ix:
            sw = int(b[ix["context_switches"]]) - int(a[ix["context_switches"]])
        if core_scoped:
            ok, why = core_ok, core_why
        else:
            ok, why = _segment_qualified(hdr, sw, o_flags, c_flags)
        if not ok:
            unqualified += 1
            unqualified_reasons[why] = unqualified_reasons.get(why, 0) + 1
            continue
        deltas = {}
        for name in counter_names:
            deltas[name] = int(b[ix[name]]) - int(a[ix[name]])
        # The denominator of the displacement rate is the INTERVAL's own
        # elapsed time, never the segment's. Where the segment brackets a
        # block, most of its wall time is time the context was not running,
        # and a rate over that would describe the wait rather than the
        # wakeup. Cycles retired by the wakeup path against the latency being
        # attributed is the figure that distinguishes a displaced context
        # from a stalled one.
        elapsed_ticks = (int(rows[cs_i][c_row][columns[cs_i]["t_wall"]])
                         - int(rows[os_i][o_row][columns[os_i]["t_wall"]]))
        elapsed_ns = elapsed_ticks * ns_per_s / hz if hz else 0
        rate = (deltas["cpu_cycles"] / elapsed_ns
                if "cpu_cycles" in deltas and elapsed_ns > 0 else None)
        samples.append((latency, deltas, rate))

    n_total = len(intervals_ns)
    n_attributable = len(samples)
    with_a_delta = n_total - cross_context - segment_unavailable
    qualified_fraction = (n_attributable / with_a_delta
                          if with_a_delta else 0.0)

    out["attributable_intervals"] = {
        "intervals_in_artefact": n_total,
        "cross_context_excluded": cross_context,
        "counter_segment_unavailable": segment_unavailable,
        "same_context": n_total - cross_context - segment_unavailable,
        "qualified": n_attributable,
        "unqualified_excluded": unqualified,
    }
    out["counter_segment"] = (
        dict(declared_segment,
             resolved=n_total - segment_unavailable,
             unresolved=segment_unavailable,
             note=("the counter delta is taken between these two points in "
                   "this context, bracketing its block; the interval's own "
                   "endpoints supply the timestamps only"))
        if declared_segment else None)
    out["qualification"] = {
        "qualified": n_attributable,
        "unqualified": unqualified,
        "by_reason": unqualified_reasons,
        "fraction_of_same_context": qualified_fraction,
        "floor": th.get("qualified_fraction_floor"),
        "below_floor": bool(n_total - cross_context) and
                       qualified_fraction < th.get("qualified_fraction_floor"),
        "rule": ("a segment is qualified when the backend follows the thread; "
                 "failing that, when the backend can detect preemption and no "
                 "context switch occurred inside the segment; failing both, "
                 "never"),
    }

    if n_total and not with_a_delta:
        out["refusals"].append(
            "no interval in this artefact has a counter delta: every one is "
            "bounded by events in two different contexts and the victim "
            "declares no counter segment. The difference between two "
            "contexts' counter snapshots is not a counter delta, so this "
            "metric yields no tier-1 evidence as recorded"
            if not declared_segment else
            "the victim declares a counter segment but not one interval "
            "resolved to a complete segment within a single context, so this "
            "artefact yields no tier-1 evidence")
        return out

    if out["qualification"]["below_floor"]:
        out["refusals"].append(
            "qualified fraction %.4f is below the floor %.4f; too few "
            "segments survive for the remainder to be a sample of the run"
            % (qualified_fraction, th.get("qualified_fraction_floor")))
        return out

    # ---- the split ---------------------------------------------------------

    lat = sorted(s[0] for s in samples)
    if not lat:
        out["refusals"].append("no qualified same-context intervals remain")
        return out

    outlier_threshold = pct(lat, quantile)
    nominal_threshold = (pct(lat, th.get("median_quantile"))
                         * th.get("nominal_band_factor"))

    outliers = [s for s in samples if s[0] > outlier_threshold]
    nominals = [s for s in samples if s[0] <= nominal_threshold]

    out["split"] = {
        "outlier_quantile": quantile,
        "outlier_threshold_ns": outlier_threshold,
        "nominal_band_factor": th.get("nominal_band_factor"),
        "nominal_threshold_ns": nominal_threshold,
        "n_outlier": len(outliers),
        "n_nominal": len(nominals),
        "n_samples": len(samples),
        "min_samples_per_side": th.get("counter_min_samples_per_side"),
    }

    if min(len(outliers), len(nominals)) < th.get(
            "counter_min_samples_per_side"):
        out["refusals"].append(
            "outlier population %d, nominal population %d; below the minimum "
            "of %d per side, a median is not a median"
            % (len(outliers), len(nominals),
               th.get("counter_min_samples_per_side")))
        return out

    # ---- enrichment, with the guards ---------------------------------------

    min_count = th.get("counter_min_outlier_median")
    for name in counter_names:
        o_vals = [s[1][name] for s in outliers]
        n_vals = [s[1][name] for s in nominals]
        o_med, n_med = _median(o_vals), _median(n_vals)
        entry = {
            "outlier_median": o_med,
            "nominal_median": n_med,
            "n_outlier": len(o_vals),
            "n_nominal": len(n_vals),
            "outlier_total": sum(o_vals),
            "nominal_total": sum(n_vals),
            "minimum_outlier_median": min_count,
            "is_qualification_counter": name in QUALIFICATION_COLUMNS,
            "ratio": None,
            "signal": NO_SIGNAL,
            "no_signal_reason": None,
        }
        # Order matters: the both-zero case is named explicitly rather than
        # falling through the zero-denominator case, because it is the one
        # that produced a verdict on a run with no aggressor and it should be
        # legible as itself in the output.
        if o_med == 0 and n_med == 0:
            entry["no_signal_reason"] = (
                "outlier and nominal medians are both zero: the counter did "
                "not move in either population, so there is nothing to "
                "compare")
        elif n_med == 0:
            entry["no_signal_reason"] = (
                "nominal median is zero: the ratio has no denominator. An "
                "absent denominator is not a large effect")
        elif o_med < min_count:
            entry["no_signal_reason"] = (
                "outlier median %.4g is below the minimum absolute count %.4g"
                % (o_med, min_count))
        else:
            entry["ratio"] = o_med / n_med
            entry["signal"] = OK
        out["counters"][name] = entry

    # ---- the artefact test -------------------------------------------------

    lat_all = [s[0] for s in samples]
    min_sd = th.get("correlation_min_stddev")
    for name in counter_names:
        vals = [s[1][name] for s in samples]
        rp = _pearson(lat_all, vals, min_sd)
        rs = _spearman(lat_all, vals, min_sd)
        e = out["counters"][name]
        e["linear_correlation"] = rp
        e["rank_correlation"] = rs
        e["correlation_n"] = len(vals)
        e["artefact"] = bool(
            rp is not None and rs is not None
            and abs(rp) >= th.get("artefact_linear_min")
            and abs(rs) < th.get("artefact_rank_max"))
        e["artefact_test"] = {
            "linear_minimum": th.get("artefact_linear_min"),
            "rank_maximum": th.get("artefact_rank_max"),
            "meaning": ("a strong line with no monotone relationship beneath "
                        "it is a few extreme points dragging a fit, and may "
                        "not carry a finding"),
        }

    # ---- displacement ------------------------------------------------------

    o_rates = [s[2] for s in outliers if s[2] is not None]
    n_rates = [s[2] for s in nominals if s[2] is not None]
    o_rate, n_rate = _median(o_rates), _median(n_rates)
    ratio = (o_rate / n_rate) if (o_rate is not None and n_rate) else None
    reading = None
    if ratio is not None:
        if ratio < th.get("displacement_off_cpu_max"):
            reading = "off-CPU: the context was displaced during its outliers"
        elif ratio < th.get("displacement_moderate_max"):
            reading = "mixed: some displacement, not clean"
        elif ratio > th.get("displacement_stall_min"):
            reading = ("on-CPU and stalled: the retired-cycle rate rises in "
                       "outliers")
        else:
            reading = "flat: neither displacement nor stall is indicated"
    out["displacement"] = {
        "outlier_median_cycles_per_ns": o_rate,
        "nominal_median_cycles_per_ns": n_rate,
        "ratio": ratio,
        "n_outlier": len(o_rates),
        "n_nominal": len(n_rates),
        "reading": reading,
        "bands": {
            "off_cpu_below": th.get("displacement_off_cpu_max"),
            "moderate_below": th.get("displacement_moderate_max"),
            "stall_above": th.get("displacement_stall_min"),
        },
        "note": ("retired cycles against elapsed time. A fall in outliers "
                 "means the context was off-CPU; flat or rising means it was "
                 "on-CPU and stalled. The two lead to different countermeasure "
                 "classes, so the figure is reported whether or not it moves"),
    }
    out["question_counters"] = question_counters
    return out


def summarise(ev, stream=sys.stdout):
    """Human-readable rendering of the structure above. Presentation only —
    it adds nothing and decides nothing."""
    p = lambda *a: print(*a, file=stream)
    p("%s  metric=%s  rule=%s  counters=%s"
      % (os.path.basename(ev["artefact_path"]), ev["metric"],
         ev["rule"]["name"], ",".join(ev["counter_state"])))
    for r in ev["refusals"]:
        p("  REFUSED: %s" % r)
    a = ev.get("attributable_intervals")
    if a:
        p("  intervals=%d cross-context=%d no-segment=%d qualified=%d "
          "unqualified=%d"
          % (a["intervals_in_artefact"], a["cross_context_excluded"],
             a.get("counter_segment_unavailable", 0),
             a["qualified"], a["unqualified_excluded"]))
        if ev.get("counter_segment"):
            cs = ev["counter_segment"]
            p("    counter segment: context '%s', points %s->%s, %d resolved"
              % (cs.get("context"), cs["open_point_id"],
                 cs["close_point_id"], cs["resolved"]))
        for why, n in sorted(ev["qualification"]["by_reason"].items()):
            p("    unqualified: %-52s %d" % (why, n))
    if ev["split"]:
        s = ev["split"]
        p("  split at P%g = %.0f ns; nominal <= %.0f ns; outlier n=%d nominal n=%d"
          % (s["outlier_quantile"] * 100, s["outlier_threshold_ns"],
             s["nominal_threshold_ns"], s["n_outlier"], s["n_nominal"]))
    if ev["counters"]:
        p("  %-20s %12s %12s %9s  %7s %7s %s"
          % ("counter", "nominal med", "outlier med", "ratio", "linear",
             "rank", "note"))
        for name in sorted(ev["counters"]):
            c = ev["counters"][name]
            ratio = "%9.3f" % c["ratio"] if c["ratio"] is not None else \
                    "%9s" % NO_SIGNAL
            lin = "%7.3f" % c["linear_correlation"] \
                if c.get("linear_correlation") is not None else "%7s" % "-"
            rnk = "%7.3f" % c["rank_correlation"] \
                if c.get("rank_correlation") is not None else "%7s" % "-"
            note = []
            if c.get("artefact"):
                note.append("ARTEFACT")
            if c["signal"] == NO_SIGNAL and c["no_signal_reason"]:
                note.append(c["no_signal_reason"])
            if c["is_qualification_counter"]:
                note.append("(qualification counter)")
            p("  %-20s %12.4g %12.4g %s  %s %s %s"
              % (name, c["nominal_median"], c["outlier_median"], ratio,
                 lin, rnk, "; ".join(note)))
    d = ev["displacement"]
    if d and d["ratio"] is not None:
        p("  displacement ratio %.4f (n_out=%d n_nom=%d) — %s"
          % (d["ratio"], d["n_outlier"], d["n_nominal"], d["reading"]))


def main(argv):
    if len(argv) < 2:
        print("usage: counters.py <derived-artefact.json>... [--json]",
              file=sys.stderr)
        return 2
    as_json = "--json" in argv
    results = []
    for path in [a for a in argv[1:] if not a.startswith("--")]:
        ev = counter_evidence(path)
        results.append(ev)
        if not as_json:
            summarise(ev)
            print()
    if as_json:
        print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
