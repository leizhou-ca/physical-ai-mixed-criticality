#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The differential: one condition against its own baseline.
#
# Nothing in the prior work does this. The tool this analysis re-derives its
# concepts from batched every condition into one summary table and computed
# nothing across them — the comparison was left to the reader's eye, which is
# where an aggressor's effect and the run-to-run spread look alike.
#
# THREE RULES DECIDE EVERYTHING HERE.
#
# 1. COMPARE THE REPEATS, NOT THEIR MEDIANS. A median of five repeats throws
#    away the only estimate of run-to-run variation the experiment contains,
#    and that estimate is what tells a shift from a coincidence. All repeats
#    on both sides go into the comparison, and the spread across them is
#    reported beside every figure taken from them.
#
# 2. THE REPEAT SPREAD IS THE NOISE FLOOR. A shift smaller than the
#    within-cell spread of the repeats is NOT a small effect; it is not
#    resolvable, and it is reported as not resolvable. Measured on this
#    campaign: doubling an aggressor's worker count produced no resolvable
#    change in five of five pairs, two of them negative. A tool that reported
#    those as small effects would have reported five findings where there
#    were none.
#
# 3. DISTRIBUTIONS, NOT SUBTRACTED PERCENTILES. Latency data is heavy-tailed
#    and not normal, so a subtracted 99.9th percentile is a poor summary of
#    what moved. The shift at each declared quantile is reported because it
#    is what a reader asks for, AND a comparison across the whole sample is
#    reported beside it, because the two disagree exactly where the summary
#    is misleading.
#
# A maximum is one observation. Five maxima have been seen to span 55% of
# their own median, so a maximum is never reported alone: its spread across
# the repeats goes with it, every time.
#
# Counter enrichment is differenced too, when the caller supplies it. A
# channel implicated equally on both sides is not the aggressor's doing — it
# is what the metric does. This module does not compute that evidence: it
# takes it, so that evidence modules stay independent of one another and a
# differential can be recomputed without re-reading records.
#
# Nothing here knows what a verdict is.
import bisect
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcib_derive import pct                                # noqa: E402
from thresholds import Thresholds                          # noqa: E402

# Terms of the Kolmogorov asymptotic series. Not a threshold: the series
# converges long before this, and the value cannot change a result.
KS_SERIES_TERMS = 100

RESOLVABLE = "resolvable"
NOT_RESOLVABLE = "not resolvable"
UNVERIFIABLE = "unverifiable from the artefact"


# ------------------------------------------------------------- distributions
#
# Written out rather than taken from a statistics library, for the reason the
# rest of the framework carries no third-party dependency. The constants
# inside these two functions belong to the formulae themselves — a rank sum
# subtracts n(n+1)/2, the Kolmogorov asymptote carries its own correction
# terms — and are not thresholds of this analysis.

def _ks_statistic(a, b):
    """Two-sample Kolmogorov-Smirnov statistic: the largest gap between the
    two empirical distributions. Both inputs must be sorted."""
    i = j = 0
    na, nb = len(a), len(b)
    d = 0.0
    while i < na and j < nb:
        if a[i] <= b[j]:
            i += 1
        else:
            j += 1
        gap = abs(i / na - j / nb)
        if gap > d:
            d = gap
    return d


def _ks_pvalue(d, na, nb):
    """Asymptotic significance of a KS statistic.

    Reported, but deliberately not leaned on: with hundreds of thousands of
    samples a side, almost any difference is significant, and significance
    says nothing about size. Resolvability against the repeat spread is what
    decides whether a shift is real here."""
    ne = (na * nb) / (na + nb)
    lam = (ne ** 0.5 + 0.12 + 0.11 / ne ** 0.5) * d
    total, sign = 0.0, 1
    for k in range(1, KS_SERIES_TERMS + 1):
        term = sign * math.exp(-2.0 * k * k * lam * lam)
        total += term
        sign = -sign
    p = 2.0 * total
    return max(0.0, min(1.0, p))


def _probability_of_superiority(a, b):
    """P(a random sample from `a` exceeds one from `b`), ties counted half.

    0.5 means the two are interleaved. This is a size, not a significance:
    it says how far apart the distributions are in a way that a reader can
    interpret without knowing the sample size. Both inputs must be sorted."""
    na, nb = len(a), len(b)
    if not na or not nb:
        return None
    # Walked over DISTINCT values rather than over every sample. Latency
    # values are quantised by the time source, so a sample of this size holds
    # long runs of identical numbers; comparing sample against sample would
    # rescan each of those runs once per member of it, which on a real cell
    # is hundreds of millions of comparisons for an answer that two binary
    # searches per distinct value give exactly.
    greater = 0.0
    equal = 0.0
    i = 0
    while i < na:
        x = a[i]
        j = i
        while j < na and a[j] == x:
            j += 1
        count_a = j - i
        lo = bisect.bisect_left(b, x)
        hi = bisect.bisect_right(b, x)
        greater += count_a * lo
        equal += count_a * (hi - lo)
        i = j
    return (greater + equal / 2.0) / (na * nb)


# -------------------------------------------------------------- preconditions

def _values_path(artefact_path):
    return artefact_path.replace(".json", ".values.csv")


def _load_side(paths):
    """Each repeat's artefact and its values, kept separate.

    Separate because the spread across repeats is the noise floor and cannot
    be recovered once they are pooled."""
    repeats = []
    for p in paths:
        art = json.load(open(p))
        vp = _values_path(p)
        with open(vp) as f:
            next(f)
            vals = [int(line) for line in f if line.strip()]
        repeats.append({"artefact_path": p, "artefact": art,
                        "values": sorted(vals)})
    return repeats


CLOCK_MET = "met"
CLOCK_FAILED = "failed"
CLOCK_UNVERIFIABLE = "unverifiable"


def _clock_state(repeats):
    """The clock-state precondition, read from the artefacts' conditions.

    The derived-interval artefact carries its run's conditions forward for
    exactly this check, and it is the check this project most
    needs: an unpinned governor has been measured on this board dominating
    per-stressor differences outright, so a differential that cannot see the
    clock is a differential that assumes it.

    Three outcomes, and the third is not the second. An artefact whose
    conditions say the clock was pinned at maximum, before and after, meets
    it. One whose conditions say it was not, fails it. One that carries no
    conditions, or carries conditions that never recorded the frequency, is
    UNVERIFIABLE — and stays unverifiable however many of its siblings are
    fine, because the question is about this run's clock and nothing else
    can answer it."""
    per, verdicts = [], set()
    for r in repeats:
        name = os.path.basename(r["artefact_path"])
        cond = r["artefact"].get("conditions")
        if not cond or cond.get("source") == "absent":
            per.append({"artefact": name, "state": CLOCK_UNVERIFIABLE,
                        "detail": "the artefact carries no conditions block"})
            verdicts.add(CLOCK_UNVERIFIABLE)
            continue
        readings, scopes = [], []
        for moment in ("before", "after"):
            cap = cond.get(moment)
            if not cap:
                readings.append(None)
                continue
            clk = cap.get("clock") or {}
            readings.append(clk.get("pinned_at_max"))
            if clk.get("pinned_at_max_scope"):
                scopes.append(clk["pinned_at_max_scope"])
        if any(v is False for v in readings):
            state = CLOCK_FAILED
        elif all(v is True for v in readings) and readings:
            state = CLOCK_MET
        else:
            state = CLOCK_UNVERIFIABLE
        per.append({"artefact": name, "state": state,
                    "before": readings[0] if readings else None,
                    "after": readings[1] if len(readings) > 1 else None,
                    "conditions_source": cond.get("source"),
                    "scope": sorted(set(scopes)) or None})
        verdicts.add(state)

    if CLOCK_FAILED in verdicts:
        bad = [p["artefact"] for p in per if p["state"] == CLOCK_FAILED]
        return {"state": CLOCK_FAILED, "per_artefact": per,
                "statement": ("the clock was not pinned at maximum for %s; a "
                              "per-stressor comparison taken with the "
                              "governor free has already been found on this "
                              "project to measure the governor rather than "
                              "the stressor" % ", ".join(bad))}
    if CLOCK_UNVERIFIABLE in verdicts:
        bad = [p["artefact"] for p in per
               if p["state"] == CLOCK_UNVERIFIABLE]
        return {"state": CLOCK_UNVERIFIABLE, "per_artefact": per,
                "statement": ("the clock state cannot be read for %d of %d "
                              "artefacts (%s); their conditions do not record "
                              "it" % (len(bad), len(per), ", ".join(bad)))}
    scopes = sorted({s for p in per for s in (p.get("scope") or [])})
    return {"state": CLOCK_MET, "per_artefact": per,
            "statement": ("every artefact on both sides records its clock "
                          "pinned at maximum before and after its run%s"
                          % (" (recorded for %s)" % "; ".join(scopes)
                             if scopes else ""))}


def _instrument_comparable(on, off, th):
    """Whether the two sides measured with the same instrument.

    AN ABSOLUTE RULE, NOT A RELATIVE ONE, and the difference is the whole
    point. The null interval is what the instrument costs when it measures
    nothing, and the question is whether it was the same on both sides. A
    relative tolerance answers a different question — whether it varied by
    the same FRACTION — and that penalises precision exactly backwards: a
    counters-off arm whose null interval moves 90 ns out of 300 fails, while
    a counters-on arm moving 150 ns out of 2200 passes, though the second
    moved more. The instrument that changed less was the one refused.

    So the two sides are comparable when the difference between them is no
    larger than the wider of:

      - a floor covering ordinary session-to-session variation, which this
        board has been measured to have even with every setting identical;
      - either side's own spread across its repeats, where there are
        repeats. A side that varies by 80 ns between its own runs cannot
        demand the other side agree to better than 80 ns.

    The second term is the same principle the differential uses for its
    noise floor: a difference smaller than the variation within a side is
    not a difference between the sides."""
    def nulls_of(side):
        return [n for r in side
                for n in r["artefact"]["instrument"]["null_interval_p50_ns"]]
    n_on, n_off = nulls_of(on), nulls_of(off)
    if not n_on or not n_off:
        return {"comparable": None, "statement": "no null interval recorded",
                "evidence": None}

    med_on = pct(sorted(n_on), th.get("median_quantile"))
    med_off = pct(sorted(n_off), th.get("median_quantile"))
    spread_on = max(n_on) - min(n_on)
    spread_off = max(n_off) - min(n_off)
    floor = th.get("precondition_null_interval_floor_ns")
    allowance = max(floor, spread_on, spread_off)
    difference = abs(med_on - med_off)

    evidence = {
        "on_null_ns": sorted(n_on), "off_null_ns": sorted(n_off),
        "on_median_ns": med_on, "off_median_ns": med_off,
        "on_repeat_spread_ns": spread_on, "off_repeat_spread_ns": spread_off,
        "difference_ns": difference,
        "floor_ns": floor,
        "allowance_ns": allowance,
        "allowance_set_by": ("the floor" if allowance == floor else
                             "the aggressor-on side's own repeat spread"
                             if allowance == spread_on else
                             "the aggressor-off side's own repeat spread"),
    }
    if difference > allowance:
        return {
            "comparable": False, "evidence": evidence,
            "statement": (
                "the two sides' null intervals differ by %.0f ns (on %.0f ns, "
                "off %.0f ns), more than the %.0f ns allowed by %s; the "
                "instrument was not the same on both sides"
                % (difference, med_on, med_off, allowance,
                   evidence["allowance_set_by"]))}
    return {
        "comparable": True, "evidence": evidence,
        "statement": ("the two sides' null intervals differ by %.0f ns "
                      "against %.0f ns allowed by %s"
                      % (difference, allowance, evidence["allowance_set_by"]))}


def check_preconditions(on, off, thresholds=None):
    """Every precondition of a differential, checked and named.

    A failure produces no comparison, names the precondition, and does not
    degrade into a weaker claim. One precondition cannot be checked from the
    inputs at all, and says so rather than passing quietly — see
    `unverifiable` in the result."""
    th = thresholds or Thresholds()
    all_r = on + off
    failures, unverifiable, met = [], [], []

    def distinct(fn):
        return sorted({json.dumps(fn(r["artefact"]), sort_keys=True)
                       for r in all_r})

    states = distinct(lambda a: a["instrument"]["counters_enabled"])
    if len(states) > 1:
        failures.append(
            "counter state differs across the artefacts (%s). On the "
            "messaging metric the two states differ by more than the "
            "interference under study, and counters on against off reorder "
            "the metrics entirely" % ", ".join(states))

    sets_ = distinct(lambda a: a["instrument"]["counter_set"])
    if len(sets_) > 1:
        failures.append(
            "counter set differs across the artefacts (%s); a differential "
            "across different sets compares two instruments"
            % ", ".join(sets_))

    rules = distinct(lambda a: [a["rule"]["name"], a["rule"]["version"]])
    if len(rules) > 1:
        failures.append(
            "interval rule or version differs across the artefacts (%s); two "
            "defensible rules on one stream have differed by a factor of 3.3"
            % ", ".join(rules))

    domains = distinct(lambda a: a.get("tier_evidence"))
    if len(domains) > 1:
        failures.append(
            "the artefacts do not share a clock domain (%s)"
            % ", ".join(domains))

    bad_tier = [os.path.basename(r["artefact_path"]) for r in all_r
                if r["artefact"]["tier"] not in ("exact", "bounded")]
    if bad_tier:
        failures.append(
            "tier is neither exact nor bounded for %s; an interval with no "
            "established time base has no latency to attribute"
            % ", ".join(bad_tier))

    max_un = th.get("precondition_max_unmatched_fraction")
    max_rj = th.get("precondition_max_rejected_fraction")
    for r in all_r:
        p = r["artefact"]["pairing"]
        matched = p["matched"] or 1
        if p["unmatched"]["count"] / matched > max_un:
            failures.append(
                "%s: unmatched events are %.4f of matched intervals, above "
                "%.4f; the run fails rather than being averaged over"
                % (os.path.basename(r["artefact_path"]),
                   p["unmatched"]["count"] / matched, max_un))
        if p["rejected"]["count"] / matched > max_rj:
            failures.append(
                "%s: rejected events are %.4f of matched intervals, above "
                "%.4f" % (os.path.basename(r["artefact_path"]),
                          p["rejected"]["count"] / matched, max_rj))

    comparable = _instrument_comparable(on, off, th)
    if comparable["comparable"] is False:
        failures.append(comparable["statement"])
    elif comparable["comparable"] is True:
        met.append({"precondition": "instrument comparable",
                    "statement": comparable["statement"],
                    "evidence": comparable["evidence"]})

    clock = _clock_state(all_r)
    if clock["state"] == CLOCK_FAILED:
        failures.append(clock["statement"])
    elif clock["state"] == CLOCK_UNVERIFIABLE:
        unverifiable.append({
            "precondition": "same board, same clock state",
            "reason": clock["statement"],
            "consequence": ("the comparison is computed and reported, and "
                            "this precondition is reported as unchecked; it "
                            "is not reported as met"),
            "evidence": clock["per_artefact"],
        })
    else:
        met.append({
            "precondition": "same board, same clock state",
            "statement": clock["statement"],
            "evidence": clock["per_artefact"],
        })

    if len(on) < th.get("differential_min_repeats") or \
       len(off) < th.get("differential_min_repeats"):
        failures.append(
            "fewer than %d repeats on a side (on=%d, off=%d); with one repeat "
            "there is no spread, and no shift can be called resolvable"
            % (th.get("differential_min_repeats"), len(on), len(off)))

    return {"failures": failures, "unverifiable": unverifiable, "met": met,
            "passed": not failures}


# ---------------------------------------------------------------- the compare

def differential(on_paths, off_paths, thresholds=None,
                 on_counter_evidence=None, off_counter_evidence=None):
    """Compare an aggressor-on cell against its aggressor-off baseline.

    `on_paths` and `off_paths` are every repeat on each side. Counter
    evidence, if supplied, is one structure per repeat as `counters.py`
    returns it; it is differenced but not computed here.

    Returns a plain structure containing no verdict."""
    th = thresholds or Thresholds()
    on = _load_side(on_paths)
    off = _load_side(off_paths)

    out = {
        "metric": on[0]["artefact"]["metric"] if on else None,
        "on_repeats": [os.path.basename(p) for p in on_paths],
        "off_repeats": [os.path.basename(p) for p in off_paths],
        "n_on_repeats": len(on),
        "n_off_repeats": len(off),
        "preconditions": None,
        "quantiles": {},
        "distribution": None,
        "maximum": None,
        "counter_channels": {},
        "thresholds": th.declared(),
    }

    pre = check_preconditions(on, off, th)
    out["preconditions"] = pre
    if not pre["passed"]:
        return out

    pooled_on = sorted(v for r in on for v in r["values"])
    pooled_off = sorted(v for r in off for v in r["values"])
    out["n_on_samples"] = len(pooled_on)
    out["n_off_samples"] = len(pooled_off)

    # ---- shift at each declared quantile, against the repeat spread --------

    for label, q in th.get("differential_quantiles"):
        on_each = [pct(r["values"], q) for r in on]
        off_each = [pct(r["values"], q) for r in off]
        on_pooled_q = pct(pooled_on, q)
        off_pooled_q = pct(pooled_off, q)
        shift = on_pooled_q - off_pooled_q
        spread_on = max(on_each) - min(on_each)
        spread_off = max(off_each) - min(off_each)
        floor = max(spread_on, spread_off)
        out["quantiles"][label] = {
            "quantile": q,
            "on_pooled_ns": on_pooled_q,
            "off_pooled_ns": off_pooled_q,
            "shift_ns": shift,
            "on_per_repeat_ns": on_each,
            "off_per_repeat_ns": off_each,
            "on_repeat_spread_ns": spread_on,
            "off_repeat_spread_ns": spread_off,
            "noise_floor_ns": floor,
            "resolvable": RESOLVABLE if abs(shift) > floor else NOT_RESOLVABLE,
            "n_on": len(pooled_on),
            "n_off": len(pooled_off),
            "basis": ("the noise floor is the wider of the two sides' "
                      "spreads across their own repeats at this quantile; a "
                      "shift no larger than it is not resolvable, and is not "
                      "a small effect"),
        }

    # ---- the whole-sample comparison ---------------------------------------

    d = _ks_statistic(pooled_on, pooled_off)
    out["distribution"] = {
        "comparison": "two-sample Kolmogorov-Smirnov, with a probability of "
                      "superiority beside it",
        "ks_statistic": d,
        "ks_p_value": _ks_pvalue(d, len(pooled_on), len(pooled_off)),
        "alpha": th.get("distribution_alpha"),
        "probability_of_superiority": _probability_of_superiority(
            pooled_on, pooled_off),
        "neutral_band": th.get("effect_neutral_band"),
        "n_on": len(pooled_on),
        "n_off": len(pooled_off),
        "note": ("with samples this large the significance is not "
                 "informative on its own; the probability of superiority is "
                 "the size, and resolvability against the repeat spread is "
                 "what decides whether a shift is real"),
    }
    pos = out["distribution"]["probability_of_superiority"]
    band = th.get("effect_neutral_band")
    out["distribution"]["separated"] = bool(
        pos is not None and abs(pos - th.get("median_quantile")) > band)

    # ---- the maximum, never alone ------------------------------------------

    on_max = [r["values"][-1] for r in on]
    off_max = [r["values"][-1] for r in off]
    out["maximum"] = {
        "on_per_repeat_ns": on_max,
        "off_per_repeat_ns": off_max,
        "on_max_ns": max(on_max),
        "off_max_ns": max(off_max),
        "on_spread_ns": max(on_max) - min(on_max),
        "off_spread_ns": max(off_max) - min(off_max),
        "shift_of_maxima_ns": max(on_max) - max(off_max),
        "n_on_repeats": len(on_max),
        "n_off_repeats": len(off_max),
        "resolvable": (RESOLVABLE
                       if abs(max(on_max) - max(off_max)) >
                       max(max(on_max) - min(on_max),
                           max(off_max) - min(off_max))
                       else NOT_RESOLVABLE),
        "note": ("a maximum is one observation; it is reported with the "
                 "spread of the maxima across repeats, never alone"),
    }

    # ---- counter enrichment, differenced ------------------------------------

    if on_counter_evidence and off_counter_evidence:
        out["counter_channels"] = _difference_counters(
            on_counter_evidence, off_counter_evidence, th)

    return out


def _median_of(values):
    v = sorted(x for x in values if x is not None)
    return pct(v, Thresholds().get("median_quantile")) if v else None


def _difference_counters(on_ev, off_ev, th):
    """Difference the per-counter enrichment across the two sides.

    A channel enriched to the same degree with the aggressor off as with it
    on is not the aggressor's doing. Where either side has no signal, the
    channel is reported as not differenceable and the reason from each side
    is kept, because "no signal" on one side and a ratio on the other is a
    different situation from a ratio on both."""
    names = set()
    for ev in list(on_ev) + list(off_ev):
        names.update(ev.get("counters", {}))
    band = th.get("enrichment_common_mode_band")
    out = {}
    for name in sorted(names):
        on_r = [e["counters"][name]["ratio"] for e in on_ev
                if name in e.get("counters", {})]
        off_r = [e["counters"][name]["ratio"] for e in off_ev
                 if name in e.get("counters", {})]
        on_med, off_med = _median_of(on_r), _median_of(off_r)
        on_reasons = sorted({e["counters"][name]["no_signal_reason"]
                             for e in on_ev
                             if name in e.get("counters", {})
                             and e["counters"][name]["no_signal_reason"]})
        off_reasons = sorted({e["counters"][name]["no_signal_reason"]
                              for e in off_ev
                              if name in e.get("counters", {})
                              and e["counters"][name]["no_signal_reason"]})
        artefact_on = any(e["counters"][name].get("artefact") for e in on_ev
                          if name in e.get("counters", {}))
        artefact_off = any(e["counters"][name].get("artefact") for e in off_ev
                           if name in e.get("counters", {}))
        entry = {
            "on_median_ratio": on_med,
            "off_median_ratio": off_med,
            "on_ratios_per_repeat": on_r,
            "off_ratios_per_repeat": off_r,
            "n_on_repeats": len(on_r),
            "n_off_repeats": len(off_r),
            "on_no_signal_reasons": on_reasons,
            "off_no_signal_reasons": off_reasons,
            "artefact_marked_on": artefact_on,
            "artefact_marked_off": artefact_off,
            "common_mode_band": band,
            "attributable": None,
            "difference": None,
            "status": None,
        }
        if on_med is None or off_med is None or off_med == 0:
            entry["status"] = "not differenceable"
        else:
            rel = on_med / off_med
            entry["difference"] = on_med - off_med
            entry["ratio_of_ratios"] = rel
            common = (1.0 / band) <= rel <= band
            entry["attributable"] = not common
            entry["status"] = ("common mode: implicated on both sides alike, "
                               "so not the aggressor's doing" if common
                               else "differs between the sides")
        out[name] = entry
    return out


def summarise(d, stream=sys.stdout):
    p = lambda *a: print(*a, file=stream)
    p("metric=%s  on=%d repeats  off=%d repeats"
      % (d["metric"], d["n_on_repeats"], d["n_off_repeats"]))
    pre = d["preconditions"]
    for f in pre["failures"]:
        p("  PRECONDITION FAILED: %s" % f)
    for u in pre["unverifiable"]:
        p("  PRECONDITION UNCHECKED (%s): %s" % (u["precondition"], u["reason"]))
    for m in pre.get("met", []):
        p("  precondition met (%s): %s" % (m["precondition"], m["statement"]))
    if not pre["passed"]:
        p("  no comparison computed")
        return
    p("  %-7s %11s %11s %11s %11s  %s"
      % ("q", "off (us)", "on (us)", "shift (us)", "floor (us)",
         "resolvable"))
    for label in ("p50", "p99", "p999", "p9999"):
        q = d["quantiles"].get(label)
        if not q:
            continue
        p("  %-7s %11.3f %11.3f %+11.3f %11.3f  %s"
          % (label, q["off_pooled_ns"] / 1e3, q["on_pooled_ns"] / 1e3,
             q["shift_ns"] / 1e3, q["noise_floor_ns"] / 1e3, q["resolvable"]))
    m = d["maximum"]
    p("  max     off=%.3f us (spread %.3f over %d)  on=%.3f us (spread %.3f "
      "over %d)  %s"
      % (m["off_max_ns"] / 1e3, m["off_spread_ns"] / 1e3, m["n_off_repeats"],
         m["on_max_ns"] / 1e3, m["on_spread_ns"] / 1e3, m["n_on_repeats"],
         m["resolvable"]))
    dist = d["distribution"]
    p("  distribution: KS=%.4f p=%.3g  P(on>off)=%.4f  separated=%s"
      % (dist["ks_statistic"], dist["ks_p_value"],
         dist["probability_of_superiority"], dist["separated"]))
    if d["counter_channels"]:
        p("  %-20s %10s %10s  %s" % ("channel", "off ratio", "on ratio",
                                     "status"))
        for name, c in sorted(d["counter_channels"].items()):
            off_s = ("%10.3f" % c["off_median_ratio"]
                     if c["off_median_ratio"] is not None else "%10s" % "-")
            on_s = ("%10.3f" % c["on_median_ratio"]
                    if c["on_median_ratio"] is not None else "%10s" % "-")
            p("  %-20s %s %s  %s" % (name, off_s, on_s, c["status"]))


def main(argv):
    if "--off" not in argv:
        print("usage: differential.py <on.json>... --off <off.json>... "
              "[--json]", file=sys.stderr)
        return 2
    i = argv.index("--off")
    on = [a for a in argv[1:i] if not a.startswith("--")]
    off = [a for a in argv[i + 1:] if not a.startswith("--")]
    d = differential(on, off)
    if "--json" in argv:
        print(json.dumps(d, indent=2, sort_keys=True))
    else:
        summarise(d)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
