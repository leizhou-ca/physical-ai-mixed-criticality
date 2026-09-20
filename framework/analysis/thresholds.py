#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Thresholds: every tunable decision the attribution analysis makes, in one
# place, each with its default and where that default came from.
#
# WHY THIS FILE EXISTS AT ALL. An attribution result is a claim about a
# machine, and every such claim rests on numbers nobody measured: what counts
# as an outlier, how big a ratio has to be before it means anything, how few
# events are too few to believe. Scattered through the analysis as literals,
# those numbers are invisible — a reader sees the conclusion and not the
# dozen choices underneath it, and cannot tell which conclusions would
# survive a different choice. Collected here, they can be listed, moved, and
# the analysis re-run to see which findings depend on them. A result that
# flips when a threshold nobody justified moves by a factor of two is a
# finding about the threshold, not about the machine.
#
# So: NO NUMERIC LITERAL BELONGS IN ANY OTHER ANALYSIS MODULE. Array indices
# and loop arithmetic are not thresholds and are exempt; anything a result
# depends on is not.
#
# PROVENANCE IS THE POINT. Each entry says where its value came from. The
# categories are deliberately unflattering, because the interesting ones are
# the weak ones:
#
#   inherited    carried from the prior implementation whose concepts this
#                analysis re-derives. It was tuned on a different instrument,
#                so carrying it over is a hypothesis, not a justification.
#   measured     a number this campaign measured, with what was measured
#                stated in the rationale.
#   guess        nobody has justified this value. It is written down so it
#                can be argued with, and so a result that depends on it can
#                be identified as depending on it.
#   definitional the value follows from the definition of the quantity and is
#                not free to move — a median is the 0.5 quantile and a range
#                needs two points. Listed so that a reader scanning for
#                literals finds them accounted for rather than missing.

PROV_INHERITED = "inherited"
PROV_MEASURED = "measured"
PROV_GUESS = "guess"
PROV_DEFINITIONAL = "definitional"

PROVENANCE_KINDS = (PROV_INHERITED, PROV_MEASURED, PROV_GUESS,
                    PROV_DEFINITIONAL)


class Threshold(object):
    """One threshold: its default, where it came from, and why."""

    __slots__ = ("name", "default", "provenance", "rationale")

    def __init__(self, name, default, provenance, rationale):
        if provenance not in PROVENANCE_KINDS:
            raise ValueError("%s: unknown provenance %r" % (name, provenance))
        self.name = name
        self.default = default
        self.provenance = provenance
        self.rationale = rationale

    def __repr__(self):
        return "Threshold(%s=%r, %s)" % (self.name, self.default,
                                         self.provenance)


def _t(name, default, provenance, rationale):
    return Threshold(name, default, provenance, rationale)


# --------------------------------------------------------------- the registry

_ENTRIES = [

    # ---- the outlier/nominal split -----------------------------------------

    _t("outlier_quantile", 0.99, PROV_INHERITED,
       "Samples above this quantile of the metric's own latency are the "
       "outlier population. The prior implementation used this for every "
       "metric but interrupt latency; see outlier_quantile_by_metric. The "
       "quantile is a parameter of the analysis and is recorded with the "
       "result, because a ratio between two populations means nothing "
       "without the rule that split them."),

    _t("nominal_band_factor", 1.05, PROV_INHERITED,
       "The nominal population is samples at or below the median times this "
       "factor — a narrow band around typical behaviour, NOT simply "
       "everything below the outlier quantile. The distinction matters: "
       "contrasting the tail against a band that already contains most of "
       "the shoulder dilutes whatever the tail is doing."),

    # ---- tier 1: counter enrichment ----------------------------------------

    _t("counter_min_outlier_median", 10.0, PROV_GUESS,
       "A counter's enrichment ratio is computed only when its outlier "
       "median is at least this many events. Below it the counter reports no "
       "signal. The prior implementation had no such floor on counters, and "
       "that is precisely the defect: a ratio of two tiny medians is noise "
       "amplified, not a measurement. The VALUE, though, is a guess — "
       "nobody has established what counts as too few for these counters on "
       "this part."),

    _t("counter_min_samples_per_side", 30, PROV_GUESS,
       "Neither the outlier nor the nominal population may be smaller than "
       "this, or no enrichment is computed for the run at all. A median over "
       "a handful of samples is not a median. The value is a guess."),

    _t("artefact_linear_min", 0.40, PROV_INHERITED,
       "Absolute linear (Pearson) correlation at or above this, combined "
       "with a rank correlation below artefact_rank_max, marks a counter as "
       "an artefact: a few extreme points dragging a line with no monotone "
       "relationship beneath it. Tuned on a different instrument."),

    _t("artefact_rank_max", 0.15, PROV_INHERITED,
       "The rank (Spearman) correlation below which the artefact marking "
       "applies. Tuned on a different instrument."),

    _t("correlation_min_stddev", 1e-9, PROV_INHERITED,
       "A column whose standard deviation is below this is treated as "
       "constant and gets no correlation rather than a division by zero. "
       "This is a numerical guard, not a finding threshold."),

    # ---- tier 1: displacement ----------------------------------------------

    _t("displacement_off_cpu_max", 0.70, PROV_INHERITED,
       "Retired-cycle rate in outliers divided by the rate in nominals. "
       "Below this the measured context was off-CPU during its outliers — "
       "something else ran. Distinguishes a context that was displaced from "
       "one that was on-CPU and stalled, and the two lead to different "
       "countermeasures."),

    _t("displacement_moderate_max", 0.85, PROV_INHERITED,
       "Between this and displacement_off_cpu_max the displacement signal is "
       "mixed rather than clean."),

    _t("displacement_stall_min", 1.20, PROV_INHERITED,
       "Above this the retired-cycle rate RISES in outliers: the context was "
       "running throughout and stalled, which points at the memory system "
       "rather than at preemption."),

    # ---- tier 1: qualification ---------------------------------------------

    _t("qualified_fraction_floor", 0.90, PROV_GUESS,
       "A run whose qualified segment fraction is below this yields no "
       "counter evidence at all. Unqualified segments are excluded from "
       "enrichment and correlation wherever they occur; this floor is the "
       "point at which so few are left that the survivors are no longer a "
       "sample of the run. The value is a guess. It bears directly on "
       "per-CPU counter backends, where a segment containing a context "
       "switch counted something other than the measured context and half "
       "the segments can contain one structurally."),

    # ---- tier 2: trace enrichment ------------------------------------------

    _t("trace_min_outlier_events", 3, PROV_INHERITED,
       "An event name needs at least this many occurrences inside outlier "
       "intervals before an enrichment is computed for it. Carried over as "
       "the prior implementation's own reliability filter."),

    _t("trace_min_total_events", 10, PROV_INHERITED,
       "And at least this many occurrences across outlier and nominal "
       "intervals together."),

    _t("trace_coverage_floor", 0.10, PROV_GUESS,
       "The fraction of outlier intervals that must contain at least one "
       "occurrence of an event before that event's enrichment is treated as "
       "explaining anything. Coverage is reported with every ratio "
       "regardless; this floor is what separates an explanation from a "
       "coincidence. It exists because the count filters above are not "
       "enough on their own: an event can clear both while appearing in a "
       "few percent of the outlier population, which is a fact about those "
       "few intervals and not about the tail. The value is a guess."),

    _t("trace_self_exclusion_presence", 0.99, PROV_GUESS,
       "An event occurring in at least this fraction of ALL intervals is "
       "there by construction rather than by interference — a context switch "
       "on a context-switching metric — and is excluded from enrichment. "
       "The prior implementation excluded such events by name; deriving the "
       "exclusion from the data as well catches the cases nobody listed. "
       "The value is a guess."),

    _t("trace_outside_window_fraction_max", 0.05, PROV_GUESS,
       "Events falling inside no interval are counted and discarded. If more "
       "than this fraction of the capture falls outside every interval, the "
       "collection window and the measured window disagree and the capture "
       "is reported as mismatched rather than analysed. The value is a "
       "guess."),

    # ---- the differential --------------------------------------------------

    _t("differential_quantiles", (("p50", 0.50), ("p99", 0.99),
                                  ("p999", 0.999), ("p9999", 0.9999)),
       PROV_INHERITED,
       "The quantiles at which a shift is reported. These are the ones the "
       "derived-interval artefact already carries, so the differential "
       "reports a shift wherever the artefact states a figure."),

    _t("differential_min_repeats", 2, PROV_DEFINITIONAL,
       "A spread across repeats needs at least two repeats. With one, there "
       "is no noise floor, and a shift cannot be called resolvable at all."),

    _t("enrichment_common_mode_band", 1.20, PROV_GUESS,
       "A counter channel whose enrichment ratios on the two sides of a "
       "differential differ by less than this factor is implicated equally "
       "on both and is therefore not the aggressor's doing. The value is a "
       "guess."),

    _t("distribution_alpha", 0.05, PROV_GUESS,
       "Significance level for the two-sample distribution comparison. This "
       "is convention, which is not the same as justification — with sample "
       "sizes in the hundreds of thousands almost any difference clears it, "
       "which is why the effect size is reported beside it and what a "
       "reader should rely on is resolvability against the repeat spread."),

    _t("effect_neutral_band", 0.05, PROV_GUESS,
       "The probability that a sample from the aggressor-on side exceeds one "
       "from the aggressor-off side is 0.5 under no effect. Within this band "
       "of 0.5 the two distributions are reported as not separated. The "
       "value is a guess."),

    # ---- preconditions -----------------------------------------------------

    _t("precondition_max_unmatched_fraction", 0.01, PROV_GUESS,
       "An artefact whose unmatched events exceed this fraction of its "
       "matched intervals fails its precondition rather than being averaged "
       "over. The value is a guess."),

    _t("precondition_max_rejected_fraction", 0.01, PROV_GUESS,
       "The same for events the interval rule rejected. The value is a "
       "guess."),

    _t("precondition_null_interval_tolerance", 0.10, PROV_GUESS,
       "Two artefacts are comparable instruments only if their null "
       "intervals agree within this relative tolerance. An instrument that "
       "changed between the two sides of a differential is measuring a "
       "different thing on each. The value is a guess."),

    # ---- definitional ------------------------------------------------------

    _t("median_quantile", 0.50, PROV_DEFINITIONAL,
       "The median. Named rather than written as a literal so that a reader "
       "scanning the analysis for unexplained numbers finds none."),

    _t("ns_per_second", 1000000000, PROV_DEFINITIONAL,
       "Nanoseconds in a second."),
]

DEFAULTS = dict((e.name, e) for e in _ENTRIES)

# Per-metric overrides. The interrupt metric's tail is driven by rare events,
# so the prior implementation split it at a higher quantile; every other
# metric used the common one. An override is a campaign's declaration about a
# metric, not a property of the analysis, which is why it is a separate table
# rather than a different default.
OUTLIER_QUANTILE_BY_METRIC = {
    "inlat": 0.999,
}


class Thresholds(object):
    """The set of threshold values one analysis run uses.

    Constructed with no arguments it is the defaults. `overrides` replaces
    named values outright; `scale` multiplies them. Scaling is what the
    sensitivity question needs — move each threshold by a factor and see which
    findings move with it — and it is a first-class operation here rather than
    an edit to the source, so that the run which produced a finding and the
    run which tested it are the same code.
    """

    def __init__(self, overrides=None, scale=None, registry=None):
        self._registry = dict(registry or DEFAULTS)
        self._overrides = dict(overrides or {})
        self._scale = dict(scale or {})
        for name in list(self._overrides) + list(self._scale):
            if name not in self._registry:
                raise KeyError("no such threshold: %s" % name)

    def __getitem__(self, name):
        return self.get(name)

    def get(self, name):
        if name not in self._registry:
            raise KeyError("no such threshold: %s" % name)
        value = self._overrides.get(name, self._registry[name].default)
        if name in self._scale:
            value = value * self._scale[name]
        return value

    def entry(self, name):
        return self._registry[name]

    def outlier_quantile(self, metric):
        """The declared outlier quantile for a metric.

        An explicit override of `outlier_quantile` applies to every metric,
        because that is what a caller asking the sensitivity question means;
        otherwise the per-metric table decides.
        """
        if "outlier_quantile" in self._overrides or \
           "outlier_quantile" in self._scale:
            return self.get("outlier_quantile")
        if metric in OUTLIER_QUANTILE_BY_METRIC:
            return OUTLIER_QUANTILE_BY_METRIC[metric]
        return self.get("outlier_quantile")

    def declared(self):
        """Every threshold's effective value, for recording with a result.

        A result that does not carry the thresholds it was computed under
        cannot be reproduced or argued with, so every module that returns
        evidence embeds this.
        """
        return dict((name, self.get(name)) for name in sorted(self._registry))

    def inventory(self):
        """Every entry with its default, effective value and provenance."""
        out = []
        for name in sorted(self._registry):
            e = self._registry[name]
            out.append({
                "name": name,
                "default": e.default,
                "effective": self.get(name),
                "provenance": e.provenance,
                "rationale": e.rationale,
            })
        return out

    def by_provenance(self, provenance):
        return sorted(n for n, e in self._registry.items()
                      if e.provenance == provenance)


def default_thresholds():
    return Thresholds()


def main(argv):
    """Print the inventory. The guesses are the point, so they print last and
    are counted."""
    import json
    th = Thresholds()
    if "--json" in argv:
        print(json.dumps(th.inventory(), indent=2, sort_keys=True))
        return 0
    for kind in PROVENANCE_KINDS:
        names = th.by_provenance(kind)
        print("== %s (%d)" % (kind, len(names)))
        for n in names:
            print("   %-42s %s" % (n, th.get(n)))
    print()
    print("%d thresholds, of which %d are admitted guesses."
          % (len(DEFAULTS), len(th.by_provenance(PROV_GUESS))))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
