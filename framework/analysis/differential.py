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


def check_preconditions(on, off, thresholds=None):
    """Every precondition of a differential, checked and named.

    A failure produces no comparison, names the precondition, and does not
    degrade into a weaker claim. One precondition cannot be checked from the
    inputs at all, and says so rather than passing quietly — see
    `unverifiable` in the result."""
    th = thresholds or Thresholds()
    all_r = on + off
    failures, unverifiable = [], []

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

    nulls = [n for r in all_r
             for n in r["artefact"]["instrument"]["null_interval_p50_ns"]]
    if nulls:
        lo, hi = min(nulls), max(nulls)
        tol = th.get("precondition_null_interval_tolerance")
        if lo > 0 and (hi - lo) / lo > tol:
            failures.append(
                "null intervals span %d-%d ns, a relative spread above %.4f; "
                "the instrument was not the same on both sides" % (lo, hi, tol))

    # Stated, not silently skipped. The clock-state precondition asks whether
    # the core's clock was pinned at maximum on both sides. That is recorded
    # in the run's conditions block, which the derived-interval artefact does
    # not carry and this module is therefore not able to read. Reporting it
    # as satisfied would be asserting something unchecked about the very
    # thing most able to dominate a comparison — an unpinned governor has
    # already been found to dominate per-stressor differences on this board.
    unverifiable.append({
        "precondition": "same board, same clock state",
        "reason": ("the clock state is recorded in the run's conditions "
                   "block; a derived-interval artefact does not carry one, "
                   "so this module cannot check it from its declared inputs"),
        "consequence": ("the comparison is computed and reported, and this "
                        "precondition is reported as unchecked; it is not "
                        "reported as met"),
    })

    if len(on) < th.get("differential_min_repeats") or \
       len(off) < th.get("differential_min_repeats"):
        failures.append(
            "fewer than %d repeats on a side (on=%d, off=%d); with one repeat "
            "there is no spread, and no shift can be called resolvable"
            % (th.get("differential_min_repeats"), len(on), len(off)))

    return {"failures": failures, "unverifiable": unverifiable,
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
