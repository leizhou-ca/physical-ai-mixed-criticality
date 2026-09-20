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


def characteristics(input_paths, headers, n_intervals):
    """Derived characteristics of the records, per contributing context.

    These are properties of the measured run rather than of the instrument or
    of the intervals, so they sit in their own block. They live here, beside
    the rules, for the reason the rules do: a characteristic computed in a
    report's working script is a second path to a number, and two paths drift.

    Every field is null where the counter set does not carry the event, rather
    than absent, so a consumer can tell "not measured" from "not present in
    this artefact version".

    `cycles_per_second` is deliberately a rate and not a utilisation fraction.
    Normalising it needs the core's clock, which is a conditions-block fact the
    record does not carry; baking a nominal frequency in here would produce a
    figure that is silently wrong on any other part or governor setting. A
    consumer that knows the clock divides.

    The segment figures are the per-event-pair deltas: what the counters moved
    between one recorded event and the next, in that context. For a per-CPU
    backend they cover the core rather than the context, which is why
    `segments_with_a_switch` is carried alongside — a segment spanning a
    context switch under such a backend includes whatever else ran."""
    out = {}
    for path, h in zip(input_paths, headers):
        _, cols, rows = load_record(path)
        ix = {c: i for i, c in enumerate(cols)}
        ctx = h["context"]
        hz = int(h["domain.frequency_hz"])
        d = {"cycles_per_second": None,
             "segment_cycles_p50": None,
             "context_switches_per_interval": None,
             "cpu_migrations_per_interval": None,
             "segments": max(0, len(rows) - 1),
             "segments_with_a_switch": None}

        if len(rows) >= 2:
            span = int(rows[-1][ix["t_wall"]]) - int(rows[0][ix["t_wall"]])
            secs = span / hz if hz else 0
            if "cpu_cycles" in ix and secs > 0:
                dc = int(rows[-1][ix["cpu_cycles"]]) - int(rows[0][ix["cpu_cycles"]])
                d["cycles_per_second"] = dc / secs
                seg = [int(rows[i + 1][ix["cpu_cycles"]]) - int(rows[i][ix["cpu_cycles"]])
                       for i in range(len(rows) - 1)]
                seg.sort()
                d["segment_cycles_p50"] = seg[len(seg) // 2]
            for name, key in (("context_switches", "context_switches_per_interval"),
                              ("cpu_migrations", "cpu_migrations_per_interval")):
                if name in ix and n_intervals:
                    dv = int(rows[-1][ix[name]]) - int(rows[0][ix[name]])
                    d[key] = dv / n_intervals
            if "context_switches" in ix:
                nz = 0
                prev = int(rows[0][ix["context_switches"]])
                for r in rows[1:]:
                    v = int(r[ix["context_switches"]])
                    if v != prev:
                        nz += 1
                    prev = v
                d["segments_with_a_switch"] = nz
        out[ctx] = d
    return out



# --------------------------------------------------------------- conditions
#
# THE CONDITIONS BLOCK TRAVELS INTO THE ARTEFACT. A record carries the
# conditions of the run that produced it; the artefact carries them forward,
# so a consumer holding intervals can check what they were measured under
# without reaching for a file no artefact names.
#
# The reason is one specific failure. The clock state is the variable this
# project has measured dominating a per-stressor comparison outright: a set
# of medians had to be withdrawn because the governor had been left at its
# default, and the differences between stressors turned out to be
# differences in clock frequency. An attribution tool holding an artefact
# had no way to check that, so it could only report the precondition
# unverifiable. With the conditions here it can check it.
#
# TWO SOURCES, AND THEY ARE NOT EQUIVALENT. A session run by the
# orchestrator writes a structured capture beside each record, before and
# after, with its own drift comparison. Sessions run before that existed
# appended a human-readable block to a conditions log. This reader takes
# either, and SAYS WHICH. It parses what parses and records what it could
# not find as absent. An artefact that claimed a pinned clock it cannot
# evidence would be worse than one that says it does not know: the whole
# point of the field is that a precondition binds to it.
#
# IT PARSES; IT DOES NOT DERIVE. Where the structured capture carries a
# drift comparison, that comparison is carried forward as it was computed.
# Where the source is free text, no drift is computed here — the before and
# after captures are both carried, and a consumer can see for itself what
# moved. Computing a drift the session never recorded would put a figure in
# an artefact that no measurement stands behind.

CONDITIONS_STRUCTURED = "structured-capture"
CONDITIONS_FREE_TEXT = "parsed-free-text"
CONDITIONS_ABSENT = "absent"

# Suffixes a victim appends for a second measured context. Every context of
# one run shares one set of conditions, because they are one run.
CONTEXT_SUFFIXES = ("a", "b", "server", "trigger", "waiter")

# Fields of the conditions block a consumer may ask for by name, and which
# the free-text form does not carry at all. Named here so that "absent" is a
# checked list rather than whatever a parser happened to miss.
FREE_TEXT_NEVER_CARRIED = (
    "clock.max_khz for every CPU (only the victim's is recorded)",
    "kernel.release",
    "kernel.version",
    "kernel.preemption",
    "kernel.isolation_parameters_requested",
    "kernel.isolation_parameters_active",
    "kernel.isolation_parameters_inert",
    "isolation.victim_cpu",
    "isolation.aggressor_cpus_requested",
    "isolation.aggressor_cpus_effective",
    "scheduling.rt_period_us",
    "scheduling.victim_policy",
    "scheduling.victim_priority",
    "aggressor.condition",
    "aggressor.identity",
    "aggressor.parameters",
    "aggressor.confinement_verified",
    "drift across the run",
)


def conditions_path(record_path):
    """Where the conditions for a record live.

    Derived from the record's own path rather than recorded in it, so that a
    consumer holding a record can find them without an index, and so that
    collecting a record and forgetting its conditions is not possible
    without noticing. Defined here, beside the artefact that carries the
    conditions forward, and imported by the orchestrator that writes them:
    two implementations of this rule would eventually disagree about which
    file belongs to which record."""
    base = record_path
    if base.endswith(".csv"):
        base = base[:-len(".csv")]
    parts = base.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in CONTEXT_SUFFIXES:
        base = parts[0]
    return base + ".conditions.json"


def _run_tag(record_path):
    """The run tag a record belongs to: its conditions path without the
    suffix. This is the key the free-text logs are written under."""
    p = conditions_path(record_path)
    return os.path.basename(p)[:-len(".conditions.json")]


def _free_text_logs(record_path):
    """Candidate free-text conditions logs for a record, in a fixed order.

    The record's own directory first, then its parent — a session that wrote
    records into a subdirectory left the log at the top. Sorted, so that a
    directory holding two logs resolves the same way every time."""
    out = []
    d = os.path.dirname(os.path.abspath(record_path))
    for cand in (d, os.path.dirname(d)):
        if not os.path.isdir(cand):
            continue
        for name in sorted(os.listdir(cand)):
            if name.startswith("conditions") and name.endswith(".log"):
                full = os.path.join(cand, name)
                if full not in out:
                    out.append(full)
    return out


def _parse_free_text(path):
    """Every block in a free-text conditions log, keyed by its heading.

    The format is the one the shell scripts wrote: a heading line naming the
    run and the moment, then `key: value` lines until the next heading."""
    blocks, key, body = {}, None, None
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("=== "):
                heading = line[4:].strip()
                name, _, when = heading.partition("  ")
                key = name.strip()
                body = {"_wall_time": when.strip() or None}
                blocks[key] = body
            elif body is not None and ":" in line:
                k, _, v = line.partition(":")
                body[k.strip()] = v.strip()
    return blocks


def _as_int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _free_text_capture(raw, moment):
    """One parsed free-text block, in the shape the structured capture uses
    for the fields it shares with it. Everything the free text does not
    carry is null, and is listed in the block's `absent`."""
    govs = (raw.get("governor") or "").split()
    freqs, maxes = {}, {}
    for k, v in raw.items():
        if k.startswith("freq_cpu"):
            freqs[k[len("freq_cpu"):]] = _as_int_or_none(v)
        elif k.startswith("freq_max"):
            maxes[k[len("freq_max"):]] = _as_int_or_none(v)
    shared = sorted(set(freqs) & set(maxes))
    pinned = None
    if shared:
        pinned = all(freqs[c] is not None and freqs[c] == maxes[c]
                     for c in shared)
        if govs and not all(g == "performance" for g in govs):
            pinned = False
    loadavg = (raw.get("loadavg") or "").split()
    throttled = raw.get("throttled")
    if throttled in ("n/a", "", None):
        throttled = None
    return {
        "moment": moment,
        "wall_time": raw.get("_wall_time"),
        "monotonic_ns": None,
        "clock": {
            "governor": govs or None,
            "current_khz": freqs or None,
            "max_khz": maxes or None,
            "pinned_at_max": pinned,
            "pinned_at_max_scope": (
                "cpu " + ", ".join(shared) if shared else None),
            "pinned_at_max_basis": (
                "the recorded current frequency equals the recorded maximum "
                "for every CPU the log names, and every governor it names is "
                "the performance governor" if shared else None),
        },
        "thermal": {"temperature_mc": _as_int_or_none(raw.get("temp_mC")),
                    "throttled": throttled},
        "isolation": {"isolated": raw.get("isolated"),
                      "victim_cpu": None,
                      "aggressor_cpus_effective": None},
        "scheduling": {"rt_runtime_us": _as_int_or_none(raw.get("rt_runtime")),
                       "rt_period_us": None,
                       "victim_policy": None,
                       "victim_priority": None},
        "kernel": None,
        "load": {"loadavg_1": _as_float_or_none(loadavg[0])
                             if loadavg else None,
                 "loadavg_5": _as_float_or_none(loadavg[1])
                              if len(loadavg) > 1 else None,
                 "running_or_uninterruptible": None},
        "aggressor": {"condition": None, "identity": None, "parameters": None,
                      "confinement_verified": None,
                      "processes": _as_int_or_none(
                          (raw.get("stressors") or "").split()[0]
                          if raw.get("stressors") else None),
                      "intensity": _as_int_or_none(raw.get("intensity"))},
        "also_recorded": {
            "free_mb": _as_int_or_none(raw.get("free_mb")),
            "perf_event_paranoid": _as_int_or_none(raw.get("paranoid")),
            "busy_processes": raw.get("busy_procs") or None,
            "pass": raw.get("pass"),
        },
    }


def _probe_conditions(headers):
    """The conditions the PROBE owns, taken from the records' own headers.

    The platform, the counter state and backend, and the measuring
    context's own scheduling state are conditions of the run as much as the
    governor is, and the probe is the component that observes them. They are read here rather
    than re-derived because the record header is where they were written.

    Where the contributing records disagree about a field, every value is
    kept: two contexts that ran at different priorities is a fact about the
    run, not a field to collapse."""
    def gather(key):
        vals = sorted({h[key] for h in headers if key in h})
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else vals
    return {
        "platform": gather("platform"),
        "counters_enabled": gather("counters.enabled"),
        "counter_set": gather("counters.set"),
        "counter_backend": gather("counters.backend"),
        "counter_backend_properties": gather("counters.backend_properties"),
        "victim_cpu": gather("context.cpu"),
        "victim_policy": gather("context.sched_policy"),
        "victim_priority": gather("context.sched_priority"),
        "memory_locked": gather("memory_locked"),
        "faults_verdict": gather("faults.verdict"),
        "warmup_events": gather("warmup_events"),
        "note": ("read from the records this artefact names. The "
                 "orchestrator's own captures are in the sibling fields; "
                 "these are the facts the probe is the only observer of"),
    }


def read_conditions(input_paths, headers):
    """The conditions block an artefact carries forward.

    Structured captures where the session wrote them; the free-text log
    where it did not; `absent` where neither exists. The source is always
    named, and a field the source does not carry is null with its absence
    recorded — never defaulted."""
    probe = _probe_conditions(headers)
    first = input_paths[0]
    rel_to = os.path.dirname(os.path.abspath(first))

    structured = conditions_path(os.path.abspath(first))
    if os.path.exists(structured):
        with open(structured) as f:
            pair = json.load(f)
        return {
            "source": CONDITIONS_STRUCTURED,
            "path": os.path.relpath(structured, rel_to),
            "run": pair.get("run"),
            "session": pair.get("session"),
            "before": pair.get("before"),
            "after": pair.get("after"),
            "drift": pair.get("drift"),
            "drifted": pair.get("drifted"),
            "absent": [],
            "probe": probe,
            "note": ("carried forward verbatim from the capture the session "
                     "wrote beside the record, including the drift "
                     "comparison it computed at capture time"),
        }

    tag = _run_tag(os.path.abspath(first))
    for log in _free_text_logs(first):
        blocks = _parse_free_text(log)
        before, after = blocks.get(tag + "-before"), blocks.get(tag + "-after")
        if before is None and after is None:
            continue
        return {
            "source": CONDITIONS_FREE_TEXT,
            "path": os.path.relpath(log, rel_to),
            "run": tag,
            "session": None,
            "before": _free_text_capture(before, "before") if before else None,
            "after": _free_text_capture(after, "after") if after else None,
            "drift": None,
            "drifted": None,
            "absent": list(FREE_TEXT_NEVER_CARRIED)
                      + ([] if before else ["the before capture"])
                      + ([] if after else ["the after capture"]),
            "probe": probe,
            "note": ("parsed from the human-readable conditions log this "
                     "session wrote. The fields listed in `absent` were "
                     "never recorded and are not inferred. No drift "
                     "comparison is computed: the session recorded none, "
                     "and both captures are carried so that a consumer can "
                     "see for itself what moved"),
        }

    return {
        "source": CONDITIONS_ABSENT,
        "path": None,
        "run": tag,
        "session": None,
        "before": None,
        "after": None,
        "drift": None,
        "drifted": None,
        "absent": ["every orchestrator-captured condition: no structured "
                   "capture and no conditions log names this run"],
        "probe": probe,
        "note": ("no conditions were found for this run. A precondition "
                 "that binds to them is reported unverifiable, never met"),
    }


def artefact(rule_name, rule_version, parameters, input_paths, headers,
             tier, tier_evidence, pairing, intervals_ns, metric):
    """The derived-interval artefact (one per rule application)."""
    ns = sorted(intervals_ns)
    return {
        "artefact": "mcib.derived_intervals",
        # v3 adds the conditions block, which is what a precondition about
        # the state of the machine binds to; v2 added the characteristics
        # block. Every other field is unchanged and re-derives
        # byte-identically from the same records.
        "artefact_version": 3,
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
        "conditions": read_conditions(input_paths, headers),
        "characteristics": characteristics(input_paths, headers, len(ns)),
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
