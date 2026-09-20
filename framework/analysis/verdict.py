#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The verdict: the only module in this framework that names a verdict class.
#
# Everything underneath it computes evidence and knows nothing about what a
# verdict is. That separation is not tidiness. The prior implementation put
# its verdict rules inside the module that computed its enrichment ratios,
# and the consequence was that its rules could not be changed without
# recomputing its evidence, could not be versioned against a dataset, and
# could not be inspected apart from the arithmetic they rested on. Its rules
# then issued a cache-eviction verdict, with a countermeasure attached, on a
# baseline run with no aggressor running at all. Nobody could see why,
# because the why was scattered through the arithmetic.
#
# SO: FOUR RULES AND SIX GUARDS, IN ONE FILE, EACH NAMED.
#
# The rules say what the evidence supports. The guards say when it does not,
# and every one of them exists because its absence produced a wrong answer:
#
#   baseline test         a rule that fires on the aggressor-off condition
#                         did not detect the aggressor
#   multiple comparisons  six counters across five conditions is thirty
#                         chances for one to look significant
#   instrument dominance  a verdict drawn from data that is three-quarters
#                         instrument is not defensible on arithmetic alone
#   statistic named       the 99.9th percentile and the maximum named a
#                         different worst aggressor in half the cases
#   counter state         counters on and counters off reorder the metrics
#                         entirely; a verdict never spans the two
#   metric-specific       I/O is the mildest aggressor for three metrics and
#                         the worst for the fourth
#
# `no verdict` IS A FIRST-CLASS OUTCOME. Most of the defects in the work this
# replaces came from a rule that always produced something. A cell that
# produces no verdict here has produced the correct result, and the artefact
# says what was missing rather than reporting silence.
#
# WHAT A `ceiling` MEANS, AND WHY THE REGISTRY EXISTS. "Nothing available at
# configuration level moves this" is two different statements: no isolation
# mechanism exists on this platform for this channel, or one exists and does
# not help. A reader has to be able to tell them apart, and a tool that
# consults nothing cannot. So the mechanisms are a per-platform registry,
# every verdict of `ceiling` states which mechanisms were looked for, and
# where the platform is not in the registry at all no verdict is issued —
# because naming a ceiling would then be a claim about the platform rather
# than about the measurement.
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import counters as counters_mod                           # noqa: E402
import trace as trace_mod                                 # noqa: E402
import differential as differential_mod                   # noqa: E402
from mcib_derive import content_hash, pct                 # noqa: E402
from thresholds import Thresholds                         # noqa: E402

VERDICT_ARTEFACT_KIND = "mcib.verdict"
VERDICT_ARTEFACT_VERSION = 1
TOOL_VERSION = 1

TUNE = "tune"
PARTITION = "partition"
CEILING = "ceiling"
NO_VERDICT = "no verdict"

CM_CONFIGURATION = "configuration"
CM_PARTITIONING = "partitioning"
CM_DESIGN_AROUND = "design-around"

FLAG_DOMINATED = "dominated"
FLAG_PROVISIONAL = "provisional"
FLAG_UNRESOLVABLE = "unresolvable"

RULE_1 = "1: a hardware channel is enriched in the differential"
RULE_2 = "2: no channel enriched, displacement falls"
RULE_3 = "3: no channel enriched, displacement flat, shift resolvable"
RULE_4 = "4: no resolvable shift"


# ------------------------------------------------------------ what a channel is
#
# Counter columns that are not interference channels. Retired cycles rise in
# a longer interval because the interval is longer; naming them as the
# channel a verdict rests on would be naming the metric's own definition.
# They are the displacement indicator, which rule 2 uses, and they are
# reported with every verdict — but they may not carry one.
NOT_A_CHANNEL = ("cpu_cycles",)

CHANNEL_FAMILY = {
    "l1d_cache_refill": "cache",
    "l2d_cache_refill": "cache",
    "l1d_tlb_refill": "tlb",
    "br_mis_pred": "branch-prediction",
    "br_pred": "branch-prediction",
}


# --------------------------------------------------- the isolation registry
#
# Per platform, because whether a mechanism exists is a property of the part
# and the kernel running on it, not of the analysis. Empty for the reference
# board, and empty DELIBERATELY: an entry that exists and is empty says "this
# was looked at and there is nothing", which is a different statement from a
# platform that is simply not listed.

MECHANISMS_SEARCHED = (
    {"mechanism": "cache partitioning — Arm MPAM, or a vendor cache "
                  "allocation control",
     "channels": ("cache",)},
    {"mechanism": "page colouring — cache-aware physical page allocation",
     "channels": ("cache",)},
    {"mechanism": "memory-bandwidth partitioning — Arm MPAM MBA or "
                  "equivalent",
     "channels": ("cache", "memory-bandwidth")},
    {"mechanism": "DRAM bank partitioning",
     "channels": ("memory-bandwidth",)},
    {"mechanism": "IOMMU / SMMU stream isolation for device traffic",
     "channels": ("device-dma",)},
    {"mechanism": "TLB partitioning", "channels": ("tlb",)},
    {"mechanism": "branch-predictor partitioning",
     "channels": ("branch-prediction",)},
)

PLATFORM_ISOLATION = {
    # The reference board. No cache partitioning, no page colouring, no
    # usable IOMMU controls. Recorded as an empty mapping rather than by
    # omission, so that a `ceiling` on this platform can state that the
    # mechanisms were looked for and are not there.
    "cortex-a72": {
        "available": {},
        "note": "this platform provides no cache partitioning, no page "
                "colouring and no usable IOMMU controls; every mechanism in "
                "the searched list was looked for and none is present",
    },
}

ISOLATION_NONE_ON_PLATFORM = "no mechanism exists on this platform"
ISOLATION_EXISTS = "a mechanism exists for this channel"
ISOLATION_PLATFORM_UNKNOWN = "this platform is not in the registry"


def isolation_for(platform, family):
    """What isolation exists for a channel family on a platform.

    Returns the finding, the mechanism if there is one, and the list of what
    was looked for — which goes into the verdict whether or not anything was
    found, because "we looked and there is nothing" is the useful half."""
    searched = [m["mechanism"] for m in MECHANISMS_SEARCHED
                if family in m["channels"]]
    entry = PLATFORM_ISOLATION.get(platform)
    if entry is None:
        return {"finding": ISOLATION_PLATFORM_UNKNOWN, "platform": platform,
                "mechanism": None, "searched": searched,
                "note": ("no registry entry exists for this platform, so "
                         "nothing could be looked for. Whether an isolation "
                         "mechanism exists for this channel here is unknown "
                         "to this build")}
    mech = entry["available"].get(family)
    if mech:
        return {"finding": ISOLATION_EXISTS, "platform": platform,
                "mechanism": mech, "searched": searched,
                "note": entry.get("note")}
    return {"finding": ISOLATION_NONE_ON_PLATFORM, "platform": platform,
            "mechanism": None, "searched": searched,
            "note": entry.get("note")}


# ------------------------------------------- what a kernel event can be tuned by
#
# Tier 2 names a kernel software path. Whether a configuration change
# addresses it is a property of the path, not of the measurement, so it is a
# table rather than a judgement made at the point of use. Anything not listed
# is a ceiling: an event nobody has a configuration answer for is exactly the
# case `ceiling` exists to name.

EVENT_COUNTERMEASURES = (
    ("irq:", CM_CONFIGURATION,
     "interrupt affinity — route the interrupt away from the measured core"),
    ("softirq", CM_CONFIGURATION,
     "softirq handling — the deferral policy, or ksoftirqd's priority and "
     "affinity on the measured core"),
    ("sched:sched_switch", CM_CONFIGURATION,
     "priority or affinity — something else was scheduled on the measured "
     "core"),
    ("sched:sched_waking", CM_CONFIGURATION,
     "priority or affinity — a wake-up landed on the measured core"),
)


def _event_countermeasure(event_name):
    for prefix, cls, note in EVENT_COUNTERMEASURES:
        if event_name.startswith(prefix) or prefix in event_name:
            return cls, note
    return None, None


# ------------------------------------------------------------ the instrument
#
# THE INSTRUMENT-TO-SIGNAL RATIO IS MEASURED, NOT ASSUMED. The derived
# artefact carries an in-line lower bound — the null interval against the
# median interval — which cannot see the cost of saving and restoring counter
# state when the workload's own scheduling moves the context. The full figure
# needs a counters-on and a counters-off run of the same cell, which is what
# this computes.
#
# This reads artefacts from BOTH counter states, and that is not a breach of
# the guard that forbids a verdict across states. The guard is about the
# latency being attributed. This is the cost of the instrument that measured
# it, and a counters-on/off pair is the only thing that can measure it.

def instrument_to_signal(on_state_paths, off_state_paths):
    """Counter cost as a fraction of the counters-off signal.

    Medians across repeats of each side's median interval. Measured this
    campaign: task switching 2.86, preemption 2.30, messaging 1.11,
    interrupt 0.57."""
    def med_p50(paths):
        vals = sorted(json.load(open(p))["intervals"]["p50"] for p in paths)
        return pct(vals, Thresholds().get("median_quantile")) if vals else None
    on_p50, off_p50 = med_p50(on_state_paths), med_p50(off_state_paths)
    if on_p50 is None or not off_p50:
        return {"ratio": None, "basis": "unavailable",
                "note": ("the counters-on/off pair for this cell is not "
                         "present, so only the in-line lower bound the "
                         "artefact carries is available")}
    return {
        "ratio": (on_p50 - off_p50) / off_p50,
        "counters_on_p50_ns": on_p50,
        "counters_off_p50_ns": off_p50,
        "cost_ns": on_p50 - off_p50,
        "basis": "median across repeats of each run's median interval, "
                 "counters on against counters off, same metric and same "
                 "aggressor",
        "note": "the cost of the instrument as a fraction of the signal it "
                "measures; above one, the verdict is flagged dominated",
    }


# ----------------------------------------------------------------- evidence

def _counter_evidence_for(paths, thresholds, root, cache):
    out = []
    for p in paths:
        if p not in cache:
            cache[p] = counters_mod.counter_evidence(p, thresholds, root)
        out.append(cache[p])
    return out


def _usable(evidence):
    """Counter evidence that computed something. A refusal is not evidence of
    absence: a metric whose intervals are all cross-context has no counter
    instrument at all, which is a different thing from a counter instrument
    that found nothing."""
    return [e for e in evidence if not e["refusals"] and e.get("counters")]


def _displacement(evidence):
    """The displacement reading, where every usable run agrees on it.

    Disagreement across repeats is reported as disagreement rather than
    resolved by majority: a run that was displaced and a run that was not are
    not the same cell."""
    readings = sorted({e["displacement"]["reading"] for e in evidence
                       if e.get("displacement")
                       and e["displacement"]["reading"]})
    ratios = [e["displacement"]["ratio"] for e in evidence
              if e.get("displacement")
              and e["displacement"]["ratio"] is not None]
    return {
        "readings": readings,
        "agreed": readings[0] if len(readings) == 1 else None,
        "median_ratio": (pct(sorted(ratios),
                             Thresholds().get("median_quantile"))
                         if ratios else None),
        "per_repeat_ratios": ratios,
    }


def _qualified_fraction_ok(evidence, th):
    fracs = [e["qualification"]["fraction_of_same_context"] for e in evidence
             if e.get("qualification")]
    floor = th.get("qualified_fraction_floor")
    return (bool(fracs) and all(f >= floor for f in fracs)), fracs, floor


def _resolvable_quantiles(diff):
    """Every declared quantile whose shift clears the repeat-spread floor,
    strongest first. Strength is the shift against the floor it had to
    clear, not the shift alone — a bigger shift over a bigger floor is not a
    stronger result."""
    out = []
    for label, q in sorted(diff["quantiles"].items()):
        if q["resolvable"] != differential_mod.RESOLVABLE:
            continue
        floor = q["noise_floor_ns"]
        strength = abs(q["shift_ns"]) / floor if floor else float("inf")
        out.append((strength, label, q))
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _enriched_channels(diff, counter_ev, th):
    """Channels the differential says are the aggressor's doing.

    Four conditions, all of them from the spec and none of them new: the
    channel differs between the two sides rather than being common mode; its
    enrichment on the loaded side clears the enrichment minimum; it is not
    marked an artefact on either side; and it is a channel rather than a
    counter that moves with the interval's own length."""
    out = []
    min_enrich = th.get("verdict_channel_enrichment_min")
    for name, c in sorted(diff.get("counter_channels", {}).items()):
        if name in NOT_A_CHANNEL:
            continue
        if any(e["counters"].get(name, {}).get("is_qualification_counter")
               for e in counter_ev if name in e.get("counters", {})):
            continue
        if not c.get("attributable"):
            continue
        if c.get("artefact_marked_on") or c.get("artefact_marked_off"):
            continue
        if c["on_median_ratio"] is None or \
                c["on_median_ratio"] < min_enrich:
            continue
        out.append((name, c))
    return out


# -------------------------------------------------------------- the assignment

def _enriched_events(trace_ev):
    """Kernel events that cleared every guard trace.py applies, in every
    repeat of the cell.

    The guards themselves — the minimum counts, the coverage floor, the
    self-exclusion of events that occur in every interval by construction —
    belong to the evidence module and are already applied there, per repeat.
    Two things are decided here.

    WHICH REPEATS COUNT: all of them. A cell is five runs, and the reason the
    differential compares repeats rather than their medians is that
    run-to-run variation is the only noise estimate the experiment contains.
    An event that clears its guards in one run of five is the tier-2 version
    of a shift smaller than the repeat spread. So an event is enriched only
    where it cleared its guards in EVERY repeat, and the figures reported are
    the medians across them. Unanimity introduces no threshold to tune and is
    strictly harder to satisfy than any single run.

    WHICH ONE A VERDICT NAMES: the strongest enrichment first, so that a
    verdict names the event that explains most rather than the first in
    alphabetical order."""
    per_repeat = ([trace_ev] if isinstance(trace_ev, dict)
                  else list(trace_ev or []))
    usable = [t for t in per_repeat if t and not t.get("refusals")]
    if not usable or len(usable) != len(per_repeat):
        return []
    names = set()
    for t in usable:
        names.update(t.get("events") or {})
    th = Thresholds()
    out = []
    for name in sorted(names):
        entries = [(t.get("events") or {}).get(name) for t in usable]
        if any(e is None or e.get("signal") != "ok" or e.get("self_excluded")
               for e in entries):
            continue
        enrich = sorted(e["enrichment"] for e in entries)
        cover = sorted(e.get("coverage_of_outliers") or 0.0 for e in entries)
        out.append({
            "event": name,
            "enrichment": pct(enrich, th.get("median_quantile")),
            "coverage": pct(cover, th.get("median_quantile")),
            "enrichment_per_repeat": enrich,
            "coverage_per_repeat": cover,
            "n_repeats": len(entries),
            "outlier_occurrences": sum(e.get("outlier_occurrences") or 0
                                       for e in entries),
            "nominal_occurrences": sum(e.get("nominal_occurrences") or 0
                                       for e in entries),
            "basis": ("cleared its guards in all %d repeats; the figures are "
                      "the medians across them" % len(entries)),
        })
    out.sort(key=lambda d: (-(d["enrichment"] or 0), d["event"]))
    return out



def _separated_candidates(ev):
    """Separated events that survived the differential, and those that did not.

    A separation is a property of the outlier population within one run. That
    it is the AGGRESSOR's doing is a further claim, and the counter tier has
    always had to make it: a channel implicated on both sides equally is not
    the aggressor's doing. This applies the same requirement to a separated
    event, using the difference the evidence module computed.

    Three outcomes and all three are carried into the artefact. Only the
    first supplies a candidate; the other two are reported so that a reader
    can tell a considered exclusion from an oversight."""
    sep = (ev.get("trace_difference") or {})
    if not sep.get("usable"):
        return [], [], sep.get("reason")
    candidates, rejected = [], []
    for name, d in sorted(sep.get("events", {}).items()):
        entry = dict(d, event=name)
        if d["outcome"] == trace_mod.SEPARATION_HIGHER:
            candidates.append(entry)
        else:
            rejected.append(entry)
    candidates.sort(key=lambda d: (-(d["difference"] or 0), d["event"]))
    rejected.sort(key=lambda d: (-(d["on_coverage"] or 0), d["event"]))
    return candidates, rejected, None


def _trace_complete(trace_ev):
    """Whether the cell's tier-2 evidence is complete enough to draw a
    conclusion — including a negative one.

    A refused capture is not a capture that found nothing. The trace module
    refuses one whose collection window and measured window disagree, or that
    dropped events, and it is right to: an enrichment computed over a hole is
    a number about the ring buffer. But a cell where three of five captures
    survived is a cell with partial evidence, and BOTH conclusions that could
    be drawn from it would depend on which runs happened to be usable — the
    positive one names an event on three runs out of five, and the negative
    one asserts a null result the other two never returned.

    So a cell is answered by its tier-2 evidence only when every repeat's
    capture was accepted. Otherwise the cell says what was refused and why."""
    per_repeat = ([trace_ev] if isinstance(trace_ev, dict)
                  else list(trace_ev or []))
    if not per_repeat:
        return False, []
    problems = []
    for i, t in enumerate(per_repeat, 1):
        for r in (t.get("refusals") or []):
            problems.append("repeat %d: %s" % (i, r))
    return (not problems), problems


def _assign(ev, th):
    """The four rules of the spec, in order, first match wins.

    Returns a candidate: a class, a channel, the statistic it rests on, the
    rule that fired and what it saw. The guards run afterwards and may
    withdraw it. Nothing here consults a guard, so that the record of what
    the evidence supported survives the record of why it was not issued."""
    diff = ev["differential"]
    pre = diff["preconditions"]
    if not pre["passed"]:
        return {
            "class": NO_VERDICT, "channel": None, "rule": None,
            "statistic": "none: no comparison was computed",
            "countermeasure_class": None, "countermeasure_note": None,
            "because": ["a precondition of the differential failed: %s" % f
                        for f in pre["failures"]],
        }

    counter_ev = _usable(ev["counter_evidence"]["on"]) + \
        _usable(ev["counter_evidence"]["off"])
    have_counters = bool(_usable(ev["counter_evidence"]["on"])) and \
        bool(_usable(ev["counter_evidence"]["off"]))
    qual_ok, fracs, floor = _qualified_fraction_ok(counter_ev, th)
    resolvable = _resolvable_quantiles(diff)

    # ---- rule 1 -----------------------------------------------------------
    if have_counters and qual_ok:
        enriched = _enriched_channels(diff, counter_ev, th)
        if enriched:
            name, c = enriched[0]
            family = CHANNEL_FAMILY.get(name, "unclassified")
            iso = isolation_for(ev["platform"], family)
            stat = ("median across repeats of the per-run enrichment ratio "
                    "(outlier median / nominal median at the P%g split), "
                    "differenced against the aggressor-off side"
                    % (ev["outlier_quantile"] * 100))
            if iso["finding"] == ISOLATION_EXISTS:
                return {"class": PARTITION, "channel": name, "rule": RULE_1,
                        "statistic": stat,
                        "countermeasure_class": CM_PARTITIONING,
                        "countermeasure_note": iso["mechanism"],
                        "isolation": iso,
                        "because": ["%s is enriched %.3f on the loaded side "
                                    "against %.3f on the baseline"
                                    % (name, c["on_median_ratio"],
                                       c["off_median_ratio"])]}
            if iso["finding"] == ISOLATION_NONE_ON_PLATFORM:
                return {"class": CEILING, "channel": name, "rule": RULE_1,
                        "statistic": stat,
                        "countermeasure_class": CM_DESIGN_AROUND,
                        "countermeasure_note":
                            "no isolation mechanism exists on this platform "
                            "for the %s channel; the mechanisms looked for "
                            "are listed with this verdict. This ceiling "
                            "means there is nothing to enable, not that "
                            "something was enabled and did not help"
                            % family,
                        "isolation": iso,
                        "because": ["%s is enriched %.3f on the loaded side "
                                    "against %.3f on the baseline"
                                    % (name, c["on_median_ratio"],
                                       c["off_median_ratio"])]}
            return {"class": NO_VERDICT, "channel": name, "rule": RULE_1,
                    "statistic": stat,
                    "countermeasure_class": None,
                    "countermeasure_note": None,
                    "isolation": iso,
                    "because": [
                        "%s is enriched and attributable, but this platform "
                        "(%s) is not in the isolation registry. Naming a "
                        "ceiling or a partition would be a claim about the "
                        "platform rather than about the measurement"
                        % (name, ev["platform"])]}

    # ---- rule 2 -----------------------------------------------------------
    disp = ev["displacement"]
    if have_counters and qual_ok and disp["agreed"] and \
            disp["agreed"].startswith("off-CPU"):
        return {
            "class": TUNE, "channel": "preemption or interrupt displacement",
            "rule": RULE_2,
            "statistic": ("median across repeats of the retired-cycle rate "
                          "in outlier intervals against nominal intervals"),
            "countermeasure_class": CM_CONFIGURATION,
            "countermeasure_note":
                "affinity or priority: the measured context was off-CPU "
                "during its outliers, so what ran instead is what to move",
            "because": ["displacement ratio %.4f — %s"
                        % (disp["median_ratio"], disp["agreed"])],
        }

    # ---- rule 3 -----------------------------------------------------------
    if resolvable:
        strength, label, q = resolvable[0]
        stat = ("shift at %s of the pooled intervals (%d repeats on the "
                "loaded side, %d on the baseline), against the wider of the "
                "two sides' spreads across their own repeats"
                % (label, diff["n_on_repeats"], diff["n_off_repeats"]))
        because = ["shift at %s is %+.3f us against a noise floor of %.3f us"
                   % (label, q["shift_ns"] / 1e3, q["noise_floor_ns"] / 1e3)]
        if not have_counters:
            because.append(ev["counter_evidence"]["unavailable_because"]
                           or "no counter evidence is available for this cell")
        trace = ev.get("trace")
        if trace is None:
            return {
                "class": NO_VERDICT, "channel": None, "rule": RULE_3,
                "statistic": stat,
                "countermeasure_class": None, "countermeasure_note": None,
                "because": because + [
                    "the latency shift is resolvable and no hardware channel "
                    "accounts for it, so the question passes to the kernel "
                    "software tier — for which this cell has no capture. "
                    "That is an absent instrument, not a null result, and it "
                    "may not be reported as one"],
            }
        complete, refused = _trace_complete(trace)
        if not complete:
            return {
                "class": NO_VERDICT, "channel": None, "rule": RULE_3,
                "statistic": stat,
                "countermeasure_class": None, "countermeasure_note": None,
                "trace_refusals": refused,
                "because": because + [
                    "%d of this cell's %d captures were refused, so the "
                    "software tier's evidence is incomplete: %s. A refused "
                    "capture is not a capture that found nothing, and "
                    "neither a finding nor a null result may rest on the "
                    "repeats that happened to survive"
                    % (len(refused),
                       len(trace) if isinstance(trace, list) else 1,
                       "; ".join(refused))],
            }
        enriched_events = _enriched_events(trace)
        separated, sep_rejected, sep_why = _separated_candidates(ev)
        if not enriched_events and separated:
            # An event enriched above its guards OR SEPARATED is named. The
            # statistic recorded is the coverage, which is a different kind
            # of statistic from a percentile shift or a ratio of medians, and
            # the artefact says which it is rather than leaving a reader to
            # assume the usual one.
            top = separated[0]
            cls, note = _event_countermeasure(top["event"])
            sep_stat = (
                "DIFFERENCED coverage of the outlier population: %s covers "
                "%.4f of outlier intervals with the aggressor and %.4f "
                "without it, a difference of %+.4f against this cell's own "
                "repeat spread of %.4f. This is a SEPARATION, not an "
                "enrichment — the event does not occur in the nominal "
                "population, so there is no denominator, no ratio and no "
                "infinity" % (top["event"], top["on_coverage"],
                              top["off_coverage"], top["difference"],
                              top["floor"]))
            return {
                "class": TUNE if cls else CEILING,
                "channel": top["event"], "rule": RULE_3,
                "statistic": sep_stat,
                "statistic_kind": "coverage (separation)",
                "countermeasure_class": cls or CM_DESIGN_AROUND,
                "countermeasure_note": note or
                    "no configuration change is registered for this event",
                "separated_events": separated,
                "separated_rejected": sep_rejected,
                "because": because + [
                    "%s is separated and the separation survives the "
                    "differential: coverage %.4f with the aggressor against "
                    "%.4f without, a difference of %+.4f over a floor of "
                    "%.4f (margin %.2fx)"
                    % (top["event"], top["on_coverage"], top["off_coverage"],
                       top["difference"], top["floor"],
                       top["margin_over_floor"] or 0)],
            }
        if not enriched_events and sep_rejected:
            # The event separated within the loaded runs and did not survive
            # the differential. Naming it would attribute to the aggressor a
            # property the metric has without one; saying nothing would leave
            # a reader unable to tell that it was examined.
            worst = sep_rejected[0]
            return {
                "class": NO_VERDICT, "channel": None, "rule": RULE_3,
                "statistic": stat,
                "separated_rejected": sep_rejected,
                "countermeasure_class": None, "countermeasure_note": None,
                "because": because + [
                    "%s separates within the loaded runs — %.4f of outlier "
                    "intervals, none of the nominal ones — but its coverage "
                    "is %s: %.4f with the aggressor against %.4f without, a "
                    "difference of %+.4f against a floor of %.4f. A property "
                    "the metric has with no aggressor running is not the "
                    "aggressor's doing"
                    % (worst["event"], worst["on_coverage"],
                       worst["outcome"], worst["on_coverage"],
                       worst["off_coverage"], worst["difference"],
                       worst["floor"])],
            }
        if not enriched_events and sep_why:
            return {
                "class": NO_VERDICT, "channel": None, "rule": RULE_3,
                "statistic": stat,
                "countermeasure_class": None, "countermeasure_note": None,
                "because": because + [
                    "the separation differential could not be taken: %s"
                    % sep_why],
            }
        if not enriched_events and not have_counters:
            # The software tier returned a null result and the hardware tier
            # did not run. "Unresolved by either instrument" is a claim about
            # two null results, and there is one. An instrument that was
            # switched off has not disagreed with anything.
            return {
                "class": NO_VERDICT, "channel": None, "rule": RULE_3,
                "statistic": stat,
                "countermeasure_class": None, "countermeasure_note": None,
                "null_results": {"counters": False, "trace": True},
                "because": because + [
                    "no kernel event cleared its guards, so the software "
                    "tier returned a null result — but the hardware tier did "
                    "not run for this cell, so the evidence does not support "
                    "'unresolved by either instrument'. One null result is "
                    "not two"],
            }
        if not enriched_events:
            return {
                "class": CEILING, "channel": None,
                "rule": RULE_3, "statistic": stat,
                "countermeasure_class": CM_DESIGN_AROUND,
                "countermeasure_note":
                    "both instruments ran and both returned null results: no "
                    "hardware channel is enriched and no kernel event is "
                    "enriched above its guards. A non-preemptible section "
                    "that simply runs long emits nothing, and that is what "
                    "this looks like",
                "because": because + [
                    "no hardware channel is enriched and no kernel event "
                    "cleared its guards; a kernel path that neither "
                    "instrument resolves"],
                "null_results": {"counters": True, "trace": True},
            }
        top = enriched_events[0]
        cls, note = _event_countermeasure(top["event"])
        return {
            "class": TUNE if cls else CEILING,
            "channel": top["event"], "rule": RULE_3, "statistic": stat,
            "countermeasure_class": cls or CM_DESIGN_AROUND,
            "countermeasure_note": note or
                "no configuration change is registered for this event",
            "because": because + [
                "%s is enriched %.3f in outlier intervals and covers %.4f of "
                "the outlier population"
                % (top["event"], top["enrichment"], top["coverage"])],
        }

    # ---- rule 4 -----------------------------------------------------------
    floors = sorted((q["noise_floor_ns"], label)
                    for label, q in diff["quantiles"].items())
    return {
        "class": NO_VERDICT, "channel": None, "rule": RULE_4,
        "statistic": ("shift at every declared quantile, against the wider "
                      "of the two sides' repeat spreads"),
        "countermeasure_class": None, "countermeasure_note": None,
        "flags": [FLAG_UNRESOLVABLE],
        "because": ["no declared quantile shows a shift larger than the "
                    "repeat spread it must clear"],
        "resolution_floor_ns": dict((label, q["noise_floor_ns"])
                                    for label, q in diff["quantiles"].items()),
        "resolution_floor_note":
            "the smallest floor is %.3f us at %s; a shift below it is not a "
            "small effect, it is not resolvable"
            % (floors[0][0] / 1e3, floors[0][1]) if floors else None,
    }


# ------------------------------------------------------------------- guards

def _interleave(paths):
    """Split one condition's repeats into two halves for the baseline test.

    Interleaved rather than cut in the middle, so that whatever drifted over
    the session lands on both halves instead of separating them. Sorted
    first, so the split is the same every time this runs."""
    s = sorted(paths)
    return s[0::2], s[1::2]


def _baseline_test(cell, ev, th, cache, root):
    """Guard: does the same rule fire on the aggressor-off condition alone?

    The aggressor-off repeats are split in two and compared against each
    other with the identical rule. If that yields the same verdict, the
    aggressor did not cause it — this is the direct guard against the
    2026-09-17 failure, where a cache verdict was issued on a run with no
    aggressor at all."""
    a, b = _interleave(cell["off_paths"])
    need = th.get("differential_min_repeats")
    if min(len(a), len(b)) < need:
        return {"ran": False,
                "reason": ("the aggressor-off condition has %d repeats; "
                           "splitting it leaves fewer than the %d a "
                           "differential needs on a side"
                           % (len(cell["off_paths"]), need)),
                "class": None}
    sub = _cell_evidence({"metric": cell["metric"],
                          "aggressor": "the baseline against itself",
                          "counter_state": cell["counter_state"],
                          "on_paths": a, "off_paths": b,
                          "trace": None,
                          "instrument": ev["instrument"]},
                         th, cache, root)
    cand = _assign(sub, th)
    return {"ran": True, "class": cand["class"], "channel": cand["channel"],
            "rule": cand["rule"], "because": cand["because"],
            "split": {"a": [os.path.basename(p) for p in a],
                      "b": [os.path.basename(p) for p in b]}}


def _apply_dominance(candidate, ev, th):
    ratio = ev["instrument"]["ratio"]
    limit = th.get("instrument_dominance_ratio")
    if ratio is None:
        return None
    return FLAG_DOMINATED if ratio > limit else None


# ------------------------------------------------------------ one cell, whole

def _cell_evidence(cell, th, cache, root):
    on_ev = _counter_evidence_for(cell["on_paths"], th, root, cache)
    off_ev = _counter_evidence_for(cell["off_paths"], th, root, cache)
    usable_on, usable_off = _usable(on_ev), _usable(off_ev)
    unavailable = None
    if not usable_on or not usable_off:
        reasons = sorted({r for e in on_ev + off_ev for r in e["refusals"]})
        unavailable = ("; ".join(reasons) if reasons
                       else "no counter evidence was produced for this cell")
    diff = differential_mod.differential(
        cell["on_paths"], cell["off_paths"], th,
        on_counter_evidence=usable_on or None,
        off_counter_evidence=usable_off or None)
    first = json.load(open(sorted(cell["on_paths"])[0]))
    platform = ((first.get("conditions") or {}).get("probe") or {}).get(
        "platform")
    return {
        "metric": cell["metric"],
        "aggressor": cell["aggressor"],
        "counter_state": cell["counter_state"],
        "platform": platform,
        "outlier_quantile": th.outlier_quantile(cell["metric"]),
        "counter_evidence": {"on": on_ev, "off": off_ev,
                             "unavailable_because": unavailable},
        "displacement": _displacement(usable_on),
        "differential": diff,
        "trace": cell.get("trace"),
        "trace_off": cell.get("trace_off"),
        "trace_difference": trace_mod.difference_separation(
            cell.get("trace"), cell.get("trace_off"), th)
            if cell.get("trace") is not None else None,
        "instrument": cell["instrument"],
    }


def verdict_for_cell(cell, thresholds=None, cache=None, root=None):
    """The verdict artefact of one metric under one aggressor in one counter
    state. Never about an aggressor generally, and never across states."""
    th = thresholds or Thresholds()
    cache = {} if cache is None else cache
    ev = _cell_evidence(cell, th, cache, root)
    candidate = _assign(ev, th)

    flags = list(candidate.get("flags") or [])
    guards = []

    dominated = _apply_dominance(candidate, ev, th)
    guards.append({
        "guard": "instrument dominance",
        "fired": bool(dominated),
        "instrument_to_signal": ev["instrument"]["ratio"],
        "limit": th.get("instrument_dominance_ratio"),
        "effect": ("the verdict is flagged dominated: the instrument costs "
                   "more than the signal it measures" if dominated
                   else "the instrument does not exceed the signal"),
    })
    if dominated:
        flags.append(dominated)

    issued = candidate["class"] != NO_VERDICT
    baseline = {"ran": False, "reason": "no verdict was a candidate, so "
                                        "there is nothing to withhold",
                "class": None}
    if issued:
        baseline = _baseline_test(cell, ev, th, cache, root)
    withheld = None
    if issued and baseline.get("ran") and \
            baseline["class"] == candidate["class"] and \
            baseline.get("channel") == candidate.get("channel"):
        withheld = candidate
        candidate = {
            "class": NO_VERDICT, "channel": None,
            "rule": candidate["rule"],
            "statistic": candidate["statistic"],
            "countermeasure_class": None, "countermeasure_note": None,
            "because": ["the same rule applied to the aggressor-off "
                        "condition alone yields the same verdict (%s%s), so "
                        "the aggressor did not cause it"
                        % (baseline["class"],
                           " on %s" % baseline["channel"]
                           if baseline["channel"] else "")],
        }
        issued = False
    guards.append({
        "guard": "baseline test",
        "fired": bool(withheld),
        "detail": baseline,
        "effect": ("the candidate verdict was withdrawn: the aggressor-off "
                   "condition produces it on its own" if withheld
                   else "the aggressor-off condition does not produce this "
                        "verdict on its own" if baseline.get("ran")
                   else "not run: %s" % baseline.get("reason")),
    })

    guards.append({
        "guard": "counter state",
        "fired": True,
        "state": ev["counter_state"],
        "effect": ("every artefact in this comparison was measured in the "
                   "same counter state, and the state is recorded with the "
                   "verdict. Counters on and counters off reorder the "
                   "metrics entirely, so a verdict never spans the two"),
    })
    guards.append({
        "guard": "statistic named",
        "fired": True,
        "statistic": candidate["statistic"],
        "statistic_kind": candidate.get("statistic_kind",
                                        "percentile shift"),
        "effect": ("the verdict states the statistic it rests on, and which "
                   "kind of statistic it is"),
    })
    guards.append({
        "guard": "metric-specific",
        "fired": True,
        "scope": "%s under the %s aggressor, counters %s"
                 % (ev["metric"], ev["aggressor"], ev["counter_state"]),
        "effect": ("this verdict is about this metric under this aggressor. "
                   "I/O is the mildest aggressor for three of this "
                   "campaign's metrics and the worst for the fourth"),
    })
    # The multiple-comparisons guard needs the sibling conditions and is
    # applied by `apply_multiple_comparisons` once every cell in the group
    # has a candidate. Its slot is created here so that a verdict artefact
    # always carries all six.
    guards.append({
        "guard": "multiple comparisons",
        "fired": None,
        "effect": "not yet evaluated: needs the sibling conditions",
    })

    diff = ev["differential"]
    art = {
        "artefact": VERDICT_ARTEFACT_KIND,
        "artefact_version": VERDICT_ARTEFACT_VERSION,
        "cell": {
            "metric": ev["metric"],
            "aggressor": ev["aggressor"],
            "counter_state": ev["counter_state"],
            "platform": ev["platform"],
        },
        "verdict": {
            "class": candidate["class"],
            "channel": candidate["channel"],
            "statistic": candidate["statistic"],
            "statistic_kind": candidate.get("statistic_kind",
                                            "percentile shift"),
            "flags": sorted(set(flags)),
            "rule": candidate["rule"],
            "because": candidate["because"],
            "withheld_candidate": withheld,
            "resolution_floor_ns": candidate.get("resolution_floor_ns"),
            "resolution_floor_note": candidate.get("resolution_floor_note"),
            "separated_events": candidate.get("separated_events"),
            "separated_rejected": candidate.get("separated_rejected"),
            "trace_refusals": candidate.get("trace_refusals"),
        },
        "countermeasure": {
            "class": candidate["countermeasure_class"],
            "note": candidate["countermeasure_note"],
        },
        "isolation": candidate.get("isolation"),
        "guards": guards,
        "evidence": {
            "enrichment": dict(
                (os.path.basename(e["artefact_path"]),
                 {"counters": e.get("counters"),
                  "refusals": e["refusals"],
                  "split": e.get("split"),
                  "qualification": e.get("qualification")})
                for e in ev["counter_evidence"]["on"] +
                ev["counter_evidence"]["off"]),
            "counters_unavailable_because":
                ev["counter_evidence"]["unavailable_because"],
            "displacement": ev["displacement"],
            "differential": diff,
            "trace": ev["trace"],
            "trace_difference": ev.get("trace_difference"),
            "trace_note": (None if ev["trace"] is not None else
                           "no kernel-event capture exists for this cell; "
                           "the software tier did not run and its silence is "
                           "not a null result"),
            "instrument": ev["instrument"],
        },
        "inputs": _inputs_block(cell, root),
        "analysis": {
            "tool": "verdict.py",
            "tool_version": TOOL_VERSION,
            "outlier_quantile": ev["outlier_quantile"],
            "thresholds": th.declared(),
            "threshold_provenance": dict(
                (e["name"], e["provenance"]) for e in th.inventory()),
            "isolation_registry": {
                "platform": ev["platform"],
                "entry_present": ev["platform"] in PLATFORM_ISOLATION,
                "mechanisms_searched": [m["mechanism"]
                                        for m in MECHANISMS_SEARCHED],
            },
        },
    }
    return art


def _inputs_block(cell, root):
    out = []
    for side, paths in (("aggressor-on", cell["on_paths"]),
                        ("aggressor-off", cell["off_paths"])):
        for p in sorted(paths):
            out.append({
                "side": side,
                "path": os.path.relpath(p, root) if root else p,
                "content_hash": content_hash(p),
            })
    return out


# ------------------------------------------ the guard that needs its siblings

def apply_multiple_comparisons(artefacts, thresholds=None):
    """Guard: a channel implicated in one condition and in no other.

    Six counters across five conditions is thirty opportunities for one to
    look significant. Where a channel carries a verdict in exactly one of the
    conditions examined for a metric and counter state, the verdict is marked
    provisional and the fact is stated. Applied across a group rather than
    inside a cell, because a cell cannot see its siblings."""
    th = thresholds or Thresholds()
    minimum = th.get("multiple_comparison_min_conditions")
    groups = {}
    for a in artefacts:
        key = (a["cell"]["metric"], a["cell"]["counter_state"])
        groups.setdefault(key, []).append(a)

    for key, group in sorted(groups.items()):
        conditions = sorted(a["cell"]["aggressor"] for a in group)
        by_channel = {}
        for a in group:
            ch = a["verdict"]["channel"]
            if a["verdict"]["class"] == NO_VERDICT or not ch:
                continue
            by_channel.setdefault(ch, []).append(a["cell"]["aggressor"])
        for a in group:
            ch = a["verdict"]["channel"]
            where = by_channel.get(ch, [])
            slot = [g for g in a["guards"]
                    if g["guard"] == "multiple comparisons"][0]
            if len(conditions) < minimum:
                slot.update({
                    "fired": False,
                    "conditions_examined": conditions,
                    "effect": ("fewer than %d conditions were examined for "
                               "this metric and counter state, so 'implicated "
                               "here and nowhere else' is a statement about "
                               "what was not measured" % minimum)})
                continue
            lonely = bool(ch) and len(where) == 1
            slot.update({
                "fired": lonely,
                "conditions_examined": conditions,
                "channel": ch,
                "also_implicated_in": sorted(x for x in where
                                             if x != a["cell"]["aggressor"]),
                "effect": ("this channel carries a verdict in this condition "
                           "and in no other of the %d examined; the verdict "
                           "is marked provisional" % len(conditions)
                           if lonely else
                           "not applicable: this cell carries no channel "
                           "verdict" if not ch else
                           "the channel is implicated in more than one "
                           "condition")})
            if lonely:
                flags = set(a["verdict"]["flags"])
                flags.add(FLAG_PROVISIONAL)
                a["verdict"]["flags"] = sorted(flags)
    return artefacts


# ------------------------------------------------------------------ rendering

def summarise(art, stream=sys.stdout):
    """Presentation only. It derives nothing and decides nothing."""
    p = lambda *a: print(*a, file=stream)
    c, v = art["cell"], art["verdict"]
    p("%s / %s / counters %s" % (c["metric"], c["aggressor"],
                                 c["counter_state"]))
    p("  verdict     %s%s%s"
      % (v["class"], "  channel=%s" % v["channel"] if v["channel"] else "",
         "  [%s]" % ", ".join(v["flags"]) if v["flags"] else ""))
    p("  rule        %s" % (v["rule"] or "none"))
    p("  statistic   %s" % v["statistic"])
    cm = art["countermeasure"]
    if cm["class"]:
        p("  counter-    %s — %s" % (cm["class"], cm["note"]))
    for line in v["because"]:
        p("  because     %s" % line)
    if art.get("isolation"):
        iso = art["isolation"]
        p("  isolation   %s" % iso["finding"])
        for m in iso["searched"]:
            p("                looked for, not found: %s" % m)
    for g in art["guards"]:
        p("  guard %-22s %s" % (g["guard"],
                                {True: "FIRED", False: "did not fire",
                                 None: "not evaluated"}[g["fired"]]))


def main(argv):
    print("verdict.py is the rule module; it is driven by mcib_attribute.py, "
          "which discovers cells and writes artefacts.\n"
          "Verdict classes: %s, %s, %s, %s."
          % (TUNE, PARTITION, CEILING, NO_VERDICT), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
