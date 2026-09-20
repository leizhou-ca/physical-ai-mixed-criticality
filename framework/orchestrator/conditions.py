#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The conditions block: the state of the machine around one run.
#
# THIS IS AN INPUT TO DERIVATION, NOT A LOG. The shell scripts this replaces
# appended a human-readable block to a conditions file beside the records.
# That file was never referenced by any artefact, so an analysis holding an
# interval had no way to reach the conditions it was measured under — and the
# one precondition that most needs checking, whether the clock was pinned,
# was therefore the one precondition nothing could check. The conditions now
# travel with the run in a form a consumer can read, and the readable block
# is a rendering of that, not the other way round.
#
# The reason it matters is specific. The clock state is the variable this
# project has measured dominating a per-stressor comparison outright: a set
# of medians had to be withdrawn because the governor had been left at its
# default and the differences between stressors turned out to be differences
# in clock frequency. That was recoverable only because per-run frequency had
# been recorded. A dataset without it cannot be re-interpreted, only
# re-measured.
#
# WHAT IS HERE AND WHAT IS NOT. The orchestrator captures the machine's
# state: clock, thermal, isolation, scheduling policy, kernel, load and the
# aggressor. It does NOT capture the run's own start and end, the sample
# count, the measuring context's scheduling state, or the instrument's
# calibration. Those belong to the probe, which is the only component present
# at both instants of the measured loop, and they are already in the record
# it writes. Duplicating them here would create a second source for the same
# fact, and two sources drift.
#
# CAPTURED BEFORE AND AFTER EVERY RUN. A single capture cannot show that
# something moved during the run, and the things most worth knowing — a core
# dropping its frequency, a thermal limit engaging, an aggressor dying
# early — are changes rather than states.
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preconditions import _cpu_list as preconditions_cpu_list  # noqa: E402

CONDITIONS_KIND = "mcib.conditions"
CONDITIONS_VERSION = 1

# Fields where a change between the before and after capture is worth
# flagging, with how much change is worth flagging. A frequency that moved at
# all matters; a temperature that moved by a degree does not.
DRIFT_RULES = {
    "clock.governor": {"any_change": True},
    "clock.current_khz": {"any_change": True},
    "isolation.isolated": {"any_change": True},
    "scheduling.rt_runtime_us": {"any_change": True},
    "aggressor.processes": {"any_change": True},
    "thermal.throttled": {"any_change": True},
    "thermal.temperature_mc": {"tolerance": 5000},
}


# Shared with the precondition checks rather than reimplemented. Both parse
# the same kernel CPU lists out of the same files, and two parsers that
# disagree about "2-3,5" would make a precondition pass while the conditions
# block recorded something else.
from preconditions import _cpu_number                    # noqa: E402


def _cpu_list(spec):
    return sorted(preconditions_cpu_list(spec))


def capture(adapter, plan, moment, aggressor=None):
    """One capture of the machine's state. `moment` is 'before' or 'after'."""
    ad = adapter
    governors, current, maximum = {}, {}, {}
    for path in ad.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        cpu = _cpu_number(path)
        governors[cpu] = ad.read_text(path)
        current[cpu] = _as_int(ad.read_text(
            path.replace("scaling_governor", "scaling_cur_freq")))
        maximum[cpu] = _as_int(ad.read_text(
            path.replace("scaling_governor", "scaling_max_freq")))

    isolated = ad.read_text("/sys/devices/system/cpu/isolated", "") or ""
    effective = ad.read_text("%s/cpuset.cpus.effective"
                             % plan["stressor_cgroup"], None)

    loadavg = (ad.read_text("/proc/loadavg", "") or "").split()
    busy = _busy(ad)

    # ACTIVE isolation parameters, read back from the kernel — never the
    # command line. A command line entry is a request, and this board's
    # kernel rejects two of the three it is given.
    requested, active = {}, {}
    for token in (ad.read_text("/proc/cmdline", "") or "").split():
        for key in ("isolcpus", "nohz_full", "rcu_nocbs"):
            if token.startswith(key + "="):
                requested[key] = token.split("=", 1)[1]
    active["isolcpus"] = isolated
    active["nohz_full"] = ad.read_text("/sys/devices/system/cpu/nohz_full",
                                       None)
    active["rcu_nocbs"] = None

    thermal_mc = _as_int(ad.read_text("/sys/class/thermal/thermal_zone0/temp"))
    throttled = None
    if ad.which("vcgencmd"):
        try:
            rc, out, _ = ad.run(["vcgencmd", "get_throttled"], timeout=10)
            throttled = out.strip() if rc == 0 else None
        except Exception:
            throttled = None

    block = {
        "kind": CONDITIONS_KIND,
        "version": CONDITIONS_VERSION,
        "moment": moment,
        "wall_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "monotonic_ns": time.clock_gettime_ns(time.CLOCK_MONOTONIC),
        "clock": {
            "governor": governors,
            "current_khz": current,
            "max_khz": maximum,
            "pinned_at_max": all(
                current.get(c) == maximum.get(c)
                for c in governors if current.get(c) and maximum.get(c)),
        },
        "thermal": {
            "temperature_mc": thermal_mc,
            "throttled": throttled,
        },
        "isolation": {
            "isolated": isolated,
            "isolated_cpus": _cpu_list(isolated),
            "victim_cpu": plan["victim_cpu"],
            "aggressor_cpus_requested": plan["aggressor_cpus"],
            "aggressor_cpus_effective": _cpu_list(effective)
                                        if effective is not None else None,
        },
        "scheduling": {
            "rt_runtime_us": _as_int(
                ad.read_text("/proc/sys/kernel/sched_rt_runtime_us")),
            "rt_period_us": _as_int(
                ad.read_text("/proc/sys/kernel/sched_rt_period_us")),
            "victim_policy": "SCHED_FIFO",
            "victim_priority": plan["victim_priority"],
        },
        "kernel": {
            "release": os.uname().release,
            "version": os.uname().version,
            "preemption": _preemption_model(),
            "isolation_parameters_requested": requested,
            "isolation_parameters_active": active,
            "isolation_parameters_inert": sorted(
                k for k in requested
                if active.get(k) is None
                or not set(_cpu_list(requested[k])) <=
                set(_cpu_list(active.get(k) or ""))),
        },
        "load": {
            "loadavg_1": _as_float(loadavg[0]) if loadavg else None,
            "loadavg_5": _as_float(loadavg[1]) if len(loadavg) > 1 else None,
            "running_or_uninterruptible": busy,
        },
        "aggressor": aggressor or {
            "condition": "none",
            "identity": None,
            "parameters": None,
            "confinement_verified": None,
            "processes": 0,
        },
        "probe_owned_fields": (
            "the run's start and end in both clock domains, the sample "
            "count, the measuring context's own policy, priority and CPU, "
            "and the instrument's time source and calibrated cost are "
            "recorded by the probe in the event record, not here"),
    }
    return block


def _preemption_model():
    version = os.uname().version
    for token in ("PREEMPT_RT", "PREEMPT_DYNAMIC", "PREEMPT", "VOLUNTARY"):
        if token in version:
            return token
    return "unknown"


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _busy(ad):
    try:
        rc, out, _ = ad.run(["ps", "-eo", "stat,comm"], timeout=10)
    except Exception:
        return {}
    names = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0][:1] in ("R", "D") and parts[1] != "ps":
            names[parts[1]] = names.get(parts[1], 0) + 1
    return names


def _get(block, dotted):
    node = block
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def drift(before, after):
    """What changed across the run, and whether it matters.

    A run whose conditions moved is not necessarily invalid, but it is not
    comparable with one whose conditions held, and nothing downstream can
    tell the difference unless it is recorded here."""
    found = []
    for field, rule in sorted(DRIFT_RULES.items()):
        a, b = _get(before, field), _get(after, field)
        if a is None and b is None:
            continue
        if rule.get("any_change"):
            if a != b:
                found.append({"field": field, "before": a, "after": b,
                              "rule": "any change"})
        else:
            tol = rule["tolerance"]
            try:
                if abs((b or 0) - (a or 0)) > tol:
                    found.append({"field": field, "before": a, "after": b,
                                  "rule": "changed by more than %s" % tol})
            except TypeError:
                continue
    return found


def pair(before, after, tag, session):
    """The two captures and their drift, as one object written beside the
    record and named by it."""
    d = drift(before, after)
    return {
        "kind": CONDITIONS_KIND,
        "version": CONDITIONS_VERSION,
        "session": session,
        "run": tag,
        "before": before,
        "after": after,
        "drift": d,
        "drifted": bool(d),
    }


def conditions_path(record_path):
    """Where the conditions for a record live.

    Derived from the record's own path rather than recorded in it, so that a
    consumer holding a record can find them without an index, and so that
    collecting a record and forgetting its conditions is not possible without
    noticing."""
    base = record_path
    for suffix in (".csv",):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    # Strip a context suffix: every context of one run shares one set of
    # conditions, because they are one run.
    parts = base.rsplit(".", 1)
    if len(parts) == 2 and parts[1] in ("a", "b", "server", "trigger",
                                        "waiter"):
        base = parts[0]
    return base + ".conditions.json"


def write(pair_block, path):
    if os.path.exists(path):
        raise SystemExit("refusing to overwrite existing conditions: %s"
                         % path)
    with open(path, "w") as f:
        json.dump(pair_block, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def render(pair_block, stream=None):
    """The human-readable rendering. This is a VIEW of the structured block
    above, produced from it — not a separate thing written alongside it."""
    import sys
    stream = stream or sys.stdout
    p = lambda *a: print(*a, file=stream)
    b = pair_block["before"]
    p("=== %s  %s" % (pair_block["run"], b["wall_time"]))
    c = b["clock"]
    govs = sorted(set(c["governor"].values()))
    p("  clock:       governor %s, cpu%s at %s kHz (max %s)%s"
      % (",".join(str(g) for g in govs), b["isolation"]["victim_cpu"],
         c["current_khz"].get(str(b["isolation"]["victim_cpu"])),
         c["max_khz"].get(str(b["isolation"]["victim_cpu"])),
         "" if c["pinned_at_max"] else "  NOT AT MAXIMUM"))
    p("  thermal:     %s mC%s" % (b["thermal"]["temperature_mc"],
                                  ", throttled=%s" % b["thermal"]["throttled"]
                                  if b["thermal"]["throttled"] else ""))
    p("  isolation:   isolated=%s victim=%s aggressors=%s (effective)"
      % (b["isolation"]["isolated"] or "none",
         b["isolation"]["victim_cpu"],
         b["isolation"]["aggressor_cpus_effective"]))
    inert = b["kernel"]["isolation_parameters_inert"]
    if inert:
        p("  kernel:      %s  REQUESTED BUT NOT ACTIVE" % ", ".join(inert))
    p("  scheduling:  rt_runtime_us=%s victim=%s prio %s"
      % (b["scheduling"]["rt_runtime_us"], b["scheduling"]["victim_policy"],
         b["scheduling"]["victim_priority"]))
    p("  load:        %s  busy: %s"
      % (b["load"]["loadavg_1"],
         ", ".join("%s x%d" % kv for kv in
                   sorted(b["load"]["running_or_uninterruptible"].items()))
         or "nothing"))
    a = b["aggressor"]
    p("  aggressor:   %s %s  confinement %s  processes %s"
      % (a["condition"], a["parameters"] or "", a["confinement_verified"],
         a["processes"]))
    if pair_block["drifted"]:
        p("  DRIFT across the run:")
        for d in pair_block["drift"]:
            p("    %-28s %s -> %s" % (d["field"], d["before"], d["after"]))
