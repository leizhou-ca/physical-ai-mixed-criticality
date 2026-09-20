#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The attribution driver: artefacts in, verdict artefacts out.
#
# Argument handling and cell discovery. NO ANALYSIS. Every number in the
# output comes from `verdict.py` and the evidence modules underneath it; what
# happens here is working out which artefacts belong in the same comparison,
# and that is a question about file names and sessions rather than about
# machines.
#
# WHAT A CELL IS. One metric, under one aggressor, in one counter state, with
# every repeat on both sides. The aggressor-off condition of the same metric,
# state and arm is the baseline it is compared against. Nothing is compared
# across metrics, across counter states, or across sessions, because each of
# those has been measured on this campaign to change the numbers by more than
# the interference under study.
#
# WHERE THE CONDITION COMES FROM. An artefact carries its metric, its counter
# state, its rule and its conditions; it does not carry which aggressor was
# running, because the aggressor is the orchestrator's fact and the
# historical sessions never wrote it into a structured field. So the cell
# index is built from the run tag, and the two facts the artefact DOES carry
# — metric and counter state — are checked against it. A tag that disagrees
# with its own artefact is refused rather than trusted: the whole point of
# naming a cell is that everything in it is the same experiment.
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trace as trace_mod                                  # noqa: E402
import verdict as verdict_mod                              # noqa: E402
from thresholds import Thresholds                          # noqa: E402

# <metric>-<arm>-i<intensity>-r<repeat>-<condition>
TAG = re.compile(r"^(?P<metric>[a-z0-9]+)-(?P<arm>[A-Za-z0-9]+)"
                 r"-i(?P<intensity>\d+)-r(?P<repeat>\d+)"
                 r"-(?P<condition>[a-z0-9]+)$")

BASELINE_CONDITION = "baseline"

# The counter state the artefact itself reports, which is the authority. The
# arm token in a run tag is the campaign's label for the run and is not: one
# campaign used it for the counter state and another for the counter backend.
COUNTER_STATE = {"1": "on", "0": "off"}


class AttributionError(Exception):
    pass


def _counter_state(art):
    states = sorted(set(art["instrument"]["counters_enabled"]))
    if len(states) != 1:
        return "mixed"
    return COUNTER_STATE.get(states[0], states[0])


def index_cells(derived_dir, baseline=BASELINE_CONDITION):
    """Every comparable cell in a directory of derived artefacts.

    Returns the cells and, separately, everything that could not be placed in
    one — a condition with no baseline, a tag that does not parse, a tag whose
    metric disagrees with its artefact. Nothing is dropped silently: an
    artefact that is not in a cell is reported as not in a cell, because a
    dataset that quietly analysed four of its five conditions would look
    exactly like one that had only four."""
    runs, unplaced = {}, []
    for name in sorted(os.listdir(derived_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(derived_dir, name)
        tag = name[:-len(".json")]
        m = TAG.match(tag)
        if not m:
            unplaced.append({"artefact": name,
                             "reason": "the file name is not a run tag"})
            continue
        art = json.load(open(path))
        if art.get("artefact") != "mcib.derived_intervals":
            unplaced.append({"artefact": name,
                             "reason": "not a derived-interval artefact"})
            continue
        if art["metric"] != m.group("metric"):
            unplaced.append({"artefact": name,
                             "reason": "the tag names metric %s and the "
                                       "artefact names %s"
                                       % (m.group("metric"), art["metric"])})
            continue
        key = (m.group("metric"), m.group("arm"), m.group("intensity"))
        runs.setdefault(key, {}).setdefault(m.group("condition"), []).append(
            {"path": path, "repeat": int(m.group("repeat")),
             "counter_state": _counter_state(art)})

    cells, problems = [], list(unplaced)
    for key in sorted(runs):
        metric, arm, intensity = key
        conditions = runs[key]
        if baseline not in conditions:
            for cond, entries in sorted(conditions.items()):
                problems.append({
                    "artefact": "%s-%s-i%s-*-%s" % (metric, arm, intensity,
                                                    cond),
                    "reason": ("no %s condition was measured for this metric, "
                               "arm and intensity; there is nothing to "
                               "compare against" % baseline)})
            continue
        off = conditions[baseline]
        for cond, entries in sorted(conditions.items()):
            if cond == baseline:
                continue
            states = sorted({e["counter_state"] for e in entries + off})
            if len(states) != 1:
                problems.append({
                    "artefact": "%s-%s-i%s-*-%s" % (metric, arm, intensity,
                                                    cond),
                    "reason": ("the artefacts in this cell were measured in "
                               "more than one counter state (%s); a verdict "
                               "never spans the two" % ", ".join(states))})
                continue
            cells.append({
                "metric": metric, "arm": arm, "intensity": intensity,
                "aggressor": cond, "counter_state": states[0],
                "on_paths": sorted(e["path"] for e in entries),
                "off_paths": sorted(e["path"] for e in off),
            })
    return cells, problems


def _counterpart(cells, cell):
    """The same metric, intensity and aggressor in the other counter state.

    Needed for the instrument figure and for nothing else: the cost of the
    counters is the difference between a run with them and a run without,
    and no single run can show it."""
    for other in cells:
        if other is cell:
            continue
        if (other["metric"], other["intensity"], other["aggressor"]) == \
           (cell["metric"], cell["intensity"], cell["aggressor"]) and \
           other["counter_state"] != cell["counter_state"]:
            return other
    return None


def _trace_cached(artefact_path, capture_path, th, root, cache):
    """Tier-2 evidence for one artefact, computed once per run.

    The baseline of a group is the aggressor-off side of every condition in
    it, so without this its evidence would be recomputed four times over and
    the four results would have to be identical for the differential to mean
    anything. Computing it once removes the question."""
    if artefact_path not in cache:
        cache[artefact_path] = trace_mod.trace_evidence(
            artefact_path, capture_path, th, root)
    return cache[artefact_path]


def _capture_for(cell, root):
    """A kernel-event capture covering this cell's aggressor-on runs.

    Looked for beside the records, under `captures/`. Absent is the normal
    case on a dataset collected before the collector existed, and absent is
    reported as absent — the software tier having said nothing is not the
    same as the software tier having found nothing."""
    out = []
    for p in sorted(cell["on_paths"]):
        tag = os.path.basename(p)[:-len(".json")]
        cand = os.path.join(root, "captures", tag + ".capture.json")
        if os.path.exists(cand):
            out.append((p, cand))
    return out


def attribute(derived_dir, output_dir, thresholds=None, session=None,
              root=None, baseline=BASELINE_CONDITION, log=None):
    """Every cell in a dataset, each with its verdict artefact written.

    Returns the artefacts and the index problems. Pure with respect to its
    inputs: the same artefacts and the same thresholds produce the same
    files, byte for byte."""
    th = thresholds or Thresholds()
    log = log or (lambda *a: None)
    root = root or os.path.dirname(os.path.abspath(derived_dir)) or "."
    cells, problems = index_cells(derived_dir, baseline)

    if not cells:
        raise AttributionError(
            "no cell in %s can be compared against a %s condition, so no "
            "verdict was computed and nothing was written. A differential "
            "needs an aggressor-off condition measured in the same counter "
            "state; %d artefact groups were found and none had one"
            % (derived_dir, baseline, len(problems)))

    cache, trace_cache, artefacts = {}, {}, []
    for cell in cells:
        other = _counterpart(cells, cell)
        if cell["counter_state"] == "on" and other:
            instrument = verdict_mod.instrument_to_signal(
                cell["on_paths"], other["on_paths"])
        elif other:
            instrument = verdict_mod.instrument_to_signal(
                other["on_paths"], cell["on_paths"])
        else:
            instrument = verdict_mod.instrument_to_signal([], [])

        # Every repeat that has a capture, not the first one. A cell is its
        # repeats; tier 2 is asked the same question of each, and the rule
        # module requires an event to clear its guards in all of them.
        captures = _capture_for(cell, root)
        trace_ev = None
        if captures and len(captures) == len(cell["on_paths"]):
            trace_ev = [_trace_cached(a, c, th, root, trace_cache)
                        for a, c in captures]
        elif captures:
            # A partial set is not a smaller sample, it is a cell where some
            # runs were collected and some were not, and treating it as the
            # former would make a verdict depend on which runs happened to
            # have a collector attached.
            trace_ev = None

        # The aggressor-off side too: a separation has to survive the
        # differential, and that comparison needs the baseline's own tier-2
        # evidence. The baseline is shared by every condition of its group,
        # so it is computed once and reused.
        trace_off = None
        off_caps = _capture_for({"on_paths": cell["off_paths"]}, root)
        if off_caps and len(off_caps) == len(cell["off_paths"]):
            trace_off = [_trace_cached(a, c, th, root, trace_cache)
                         for a, c in off_caps]

        log("  %s / %s / counters %s" % (cell["metric"], cell["aggressor"],
                                         cell["counter_state"]))
        art = verdict_mod.verdict_for_cell(
            {"metric": cell["metric"], "aggressor": cell["aggressor"],
             "counter_state": cell["counter_state"],
             "on_paths": cell["on_paths"], "off_paths": cell["off_paths"],
             "trace": trace_ev, "trace_off": trace_off,
             "instrument": instrument},
            thresholds=th, cache=cache, root=root)
        art["cell"]["arm"] = cell["arm"]
        art["cell"]["intensity"] = int(cell["intensity"])
        art["session"] = session
        artefacts.append(art)

    verdict_mod.apply_multiple_comparisons(artefacts, th)

    os.makedirs(output_dir, exist_ok=True)
    written = []
    for art in artefacts:
        c = art["cell"]
        name = "%s-%s-i%d-%s.verdict.json" % (c["metric"], c["arm"],
                                              c["intensity"], c["aggressor"])
        path = os.path.join(output_dir, name)
        with open(path, "w") as f:
            json.dump(art, f, indent=2, sort_keys=True)
            f.write("\n")
        written.append(path)

    index = {
        "artefact": "mcib.verdict_index",
        "artefact_version": 1,
        "session": session,
        "derived": os.path.relpath(derived_dir, root),
        "cells": [{"metric": a["cell"]["metric"],
                   "arm": a["cell"]["arm"],
                   "intensity": a["cell"]["intensity"],
                   "aggressor": a["cell"]["aggressor"],
                   "counter_state": a["cell"]["counter_state"],
                   "verdict": a["verdict"]["class"],
                   "channel": a["verdict"]["channel"],
                   "flags": a["verdict"]["flags"],
                   "artefact": os.path.basename(p)}
                  for a, p in zip(artefacts, written)],
        "not_in_any_cell": problems,
        "thresholds": th.declared(),
    }
    ipath = os.path.join(output_dir, "verdicts.json")
    with open(ipath, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
    return artefacts, problems, ipath


def main(argv):
    if len(argv) < 2:
        print("usage: mcib_attribute.py <derived-dir> [-o out-dir] "
              "[--threshold name=value ...] [--session name]",
              file=sys.stderr)
        return 2
    derived = argv[1]
    out = "verdicts"
    session = None
    overrides = {}
    i = 2
    while i < len(argv):
        if argv[i] in ("-o", "--output"):
            out = argv[i + 1]; i += 2
        elif argv[i] == "--session":
            session = argv[i + 1]; i += 2
        elif argv[i] == "--threshold":
            name, _, value = argv[i + 1].partition("=")
            overrides[name] = json.loads(value)
            i += 2
        else:
            i += 1
    th = Thresholds(overrides=overrides or None)
    arts, problems, index = attribute(derived, out, th, session,
                                      log=lambda *a: print(*a))
    for a in arts:
        verdict_mod.summarise(a)
        print("")
    for p in problems:
        print("  not in any cell: %s — %s" % (p["artefact"], p["reason"]))
    print("%d verdicts, index at %s" % (len(arts), index))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
