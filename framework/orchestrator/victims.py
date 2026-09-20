#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The victim manifest.
#
# WHY A MANIFEST RATHER THAN CODE. The victims do not share an invocation and
# were never going to. Each measures a different thing, so each needs
# arguments the others have no use for, writes a different number of records,
# and pairs with a different interval rule. An orchestrator that knows those
# differences in its own code has to be edited to add a fifth victim, and
# every such edit is a chance to get one of the existing four wrong.
#
# So the differences are DATA. Adding a victim is an entry in this file: its
# binary, which options it accepts, which contexts it records and what it
# names their files, the rule its events pair under, and anything the board
# must provide before it can run at all. Nothing above this file changes.
#
# EVERY FIELD HERE WAS READ OUT OF THE VICTIM'S SOURCE, not assumed from the
# others. Four things differ in ways that would each have been a silent bug:
#
#   - Not every victim accepts every option. Only two can select a counter
#     backend; only one takes boundary kernel timestamps; two take a priority
#     gap the other two have no second priority for. A config asking a victim
#     for an option it does not have is refused when the config is read,
#     naming the victims that do have it, rather than producing a usage error
#     on the board forty minutes later.
#
#   - One victim writes its primary record to the path it was given, and the
#     other three write only suffixed files with NOTHING at the bare path.
#     Anything that assumes the given path exists afterwards is wrong for
#     three victims out of four.
#
#   - One victim's second record appears only when an option asks for it, so
#     the set of files a run produces is not a property of the victim alone.
#
#   - An iteration is not an interval. The task-switching metric is measured
#     in both directions, so one iteration of its loop yields two intervals,
#     and a run of n iterations produces 2n. Predicting how long a session
#     takes, or how much space it needs, gets this wrong by a factor of two
#     for that victim unless it is declared.

# Where a victim's counter delta comes from, when it is not the interval's own
# two endpoints. A metric whose interval is owned by a context that BLOCKS AND
# WAKES brackets its own block with two marks, and that pair — not the
# interval's endpoints — is what tier 1 differences. Declared here because it
# is a property of the victim's instrumentation, and carried into the artefact
# by the rule, so that an analysis holding an artefact knows where the delta
# came from without being told.
#
# The point ids are the victim's own. A metric with no entry here has its
# delta taken from its interval's endpoints, which is correct exactly when one
# context owns the whole interval.
PT_WAIT_ENTER = 3
PT_RESUME = 2

WAITER_BLOCK_SEGMENT = {"context": "waiter",
                        "open_point_id": PT_WAIT_ENTER,
                        "close_point_id": PT_RESUME}


RULE_TOKEN_PAIR = "token-pair"
RULE_YIELD_SUCCESSOR = "yield-successor"

# Options every victim accepts, with what the orchestrator calls them.
# Kept separate from the per-victim additions so that the common case is
# visible as common.
COMMON_OPTIONS = {
    "cpu": "-c",
    "iterations": "-n",
    "priority": "-P",
    "warmup": "-w",
    "output": "-o",
    "execution_context": "-e",
    "counter_set": "-s",
}

# Options that take no value; the orchestrator passes them when the setting
# is true (or, for counters, when the setting is false — see `counters_off`).
COMMON_FLAGS = {
    "counters_off": "-K",
    "relaxed_scheduling": "-R",
}


class Context(object):
    """One measured context inside a victim, and the file it writes.

    `suffix` is inserted before the extension of the path the victim was
    given. An empty suffix means the victim writes that context to the given
    path itself, which exactly one victim does."""

    __slots__ = ("name", "suffix", "requires_option")

    def __init__(self, name, suffix, requires_option=None):
        self.name = name
        self.suffix = suffix
        self.requires_option = requires_option

    def path_for(self, base):
        if not self.suffix:
            return base
        if "." in base.rsplit("/", 1)[-1]:
            stem, _, ext = base.rpartition(".")
            return "%s.%s.%s" % (stem, self.suffix, ext)
        return "%s.%s" % (base, self.suffix)


class HardwarePrecondition(object):
    """Something the board must provide before a victim can run at all.

    Declared rather than discovered, so that `check` can report it without
    running the victim, and so that a user who does not have it is told what
    to build rather than shown a device error."""

    __slots__ = ("name", "detail", "remedy")

    def __init__(self, name, detail, remedy):
        self.name = name
        self.detail = detail
        self.remedy = remedy


class Victim(object):
    __slots__ = ("metric", "binary", "description", "options", "flags",
                 "contexts", "rule", "rule_parameters", "counter_set",
                 "outlier_quantile", "intervals_per_iteration",
                 "hardware", "notes")

    def __init__(self, metric, binary, description, extra_options=None,
                 extra_flags=None, contexts=(), rule=None,
                 rule_parameters=None, counter_set="armv3-default",
                 outlier_quantile=0.99, intervals_per_iteration=1,
                 hardware=(), notes=""):
        self.metric = metric
        self.binary = binary
        self.description = description
        self.options = dict(COMMON_OPTIONS)
        self.options.update(extra_options or {})
        self.flags = dict(COMMON_FLAGS)
        self.flags.update(extra_flags or {})
        self.contexts = tuple(contexts)
        self.rule = rule
        self.rule_parameters = dict(rule_parameters or {})
        self.counter_set = counter_set
        self.outlier_quantile = outlier_quantile
        self.intervals_per_iteration = intervals_per_iteration
        self.hardware = tuple(hardware)
        self.notes = notes

    # -- what the orchestrator asks a manifest entry -------------------------

    def supports(self, setting):
        return setting in self.options or setting in self.flags

    def records_for(self, base, settings=None):
        """Every record path this victim will write, given these settings.

        Not a property of the victim alone: one victim's second context
        appears only when an option asks for it."""
        settings = settings or {}
        out = []
        for c in self.contexts:
            if c.requires_option and not settings.get(c.requires_option):
                continue
            out.append((c.name, c.path_for(base)))
        return out

    def primary_record(self, base, settings=None):
        recs = self.records_for(base, settings)
        return recs[0][1] if recs else None

    def command(self, binary_path, output, settings):
        """The victim's command line, built from declared options only.

        An unknown or unsupported setting raises rather than being dropped:
        a setting silently ignored is a run that measured something other
        than what was asked for, and nothing downstream would show it."""
        argv = [binary_path]
        for key in sorted(settings):
            if key in ("output",):
                continue
            value = settings[key]
            if value is None:
                continue
            if key in self.flags:
                if value:
                    argv.append(self.flags[key])
                continue
            if key in self.options:
                argv += [self.options[key], str(value)]
                continue
            raise ValueError(
                "victim %s does not accept %r; it accepts %s"
                % (self.metric, key,
                   ", ".join(sorted(set(self.options) | set(self.flags)))))
        argv += [self.options["output"], output]
        return argv

    def intervals_for(self, iterations):
        return iterations * self.intervals_per_iteration


# --------------------------------------------------------------- the manifest

MANIFEST = {}


def _add(v):
    MANIFEST[v.metric] = v
    return v


_add(Victim(
    metric="imlat",
    binary="imlat",
    description="intertask messaging latency — a pipe round trip between two "
                "threads on one core, halved to one way",
    extra_options={"counter_backend": "-B"},
    extra_flags={"boundary_timestamps": "-b",
                 "instrument_server": "-S",
                 "counters_in_server": "-C"},
    # The sender writes to the path it was given. The server's record exists
    # only when -S asked for it, and is a SEPARATE record of a separate
    # context — nothing is shared and no delta is claimed across the two.
    contexts=(Context("sender", ""),
              Context("server", "server", requires_option="instrument_server")),
    rule=RULE_TOKEN_PAIR,
    # One-way latency is the round trip halved, as the reference this metric
    # is compared against defines it.
    rule_parameters={"halve_round_trip": True},
    intervals_per_iteration=1,
    notes="the only victim whose primary record is written to the bare path, "
          "and the only one whose set of records depends on an option",
))

_add(Victim(
    metric="tslat",
    binary="tslat",
    description="voluntary task-switching latency — two equal-priority "
                "threads yielding to each other on one core",
    extra_options={"counter_backend": "-B"},
    # Symmetric contexts: neither owns the interval, so neither takes the
    # bare path and there is no file at the path the victim was given.
    contexts=(Context("a", "a"), Context("b", "b")),
    rule=RULE_YIELD_SUCCESSOR,
    rule_parameters={},
    # Measured in both directions: one iteration of the loop yields two
    # intervals, so n iterations produce 2n.
    intervals_per_iteration=2,
    notes="no token can cross a yield, so the interval is reconstructed "
          "off-target from the two streams",
))

_add(Victim(
    metric="prelat",
    binary="prelat",
    description="task preemption latency — a low-priority trigger signals, a "
                "high-priority waiter wakes and runs",
    extra_options={"priority_gap": "-g", "atomic_mode": "-A"},
    contexts=(Context("trigger", "trigger"), Context("waiter", "waiter")),
    rule=RULE_TOKEN_PAIR,
    # The interval's timestamps span the two contexts; its counter delta does
    # not. The waiter brackets its own block, so the delta is the futex-wake
    # path — the wake call, the scheduler, the resume — and nothing else.
    rule_parameters={"halve_round_trip": False,
                     "counter_segment": WAITER_BLOCK_SEGMENT},
    intervals_per_iteration=1,
    notes="cross-context but token-paired: the Nth signal causes the Nth "
          "wake, and both contexts know which iteration they are in. The "
          "counter delta is taken in the waiter, bracketing its block",
))

_add(Victim(
    metric="inlat",
    binary="inlat",
    description="interrupt handling latency — a driven GPIO edge travels a "
                "wire to a second line a waiter is blocked on",
    extra_options={"priority_gap": "-g"},
    contexts=(Context("waiter", "waiter"), Context("trigger", "trigger")),
    rule=RULE_TOKEN_PAIR,
    # As for preemption latency: the waiter brackets its own block in poll(),
    # so the delta is interrupt delivery and the wake path, not a difference
    # between two threads' counters.
    rule_parameters={"halve_round_trip": False,
                     "counter_segment": WAITER_BLOCK_SEGMENT},
    intervals_per_iteration=1,
    # The tail is driven by rare events, so it is split higher than the
    # others. Declared here because it is a property of the metric.
    outlier_quantile=0.999,
    hardware=(HardwarePrecondition(
        name="GPIO loopback",
        detail="two GPIO lines wired together: the victim drives one and "
               "blocks on an edge from the other. Continuity is checked by "
               "the victim before it measures anything.",
        remedy="connect a jumper between the two GPIO pins this victim "
               "declares, then re-run. Without the wire the victim cannot "
               "produce an edge and will refuse to run rather than record "
               "an interval that measures nothing."),),
    notes="the measured interval spans the ioctl, the pin driving, the edge, "
          "interrupt delivery and the wake path — it is not a bare interrupt "
          "delivery figure",
))


def get(metric):
    if metric not in MANIFEST:
        raise KeyError(metric)
    return MANIFEST[metric]


def metrics():
    return sorted(MANIFEST)


def victims_supporting(setting):
    return sorted(m for m, v in MANIFEST.items() if v.supports(setting))


def describe(metric, stream=None):
    import sys
    stream = stream or sys.stdout
    v = get(metric)
    p = lambda *a: print(*a, file=stream)
    p("%s — %s" % (v.metric, v.description))
    p("  binary          %s" % v.binary)
    p("  rule            %s %s" % (v.rule, v.rule_parameters or ""))
    p("  records         %s" % ", ".join(
        "%s -> %s" % (c.name, c.path_for("RUN.csv"))
        + (" (only with %s)" % c.requires_option if c.requires_option else "")
        for c in v.contexts))
    p("  intervals       %d per iteration" % v.intervals_per_iteration)
    p("  counter set     %s" % v.counter_set)
    p("  outlier split   P%g" % (v.outlier_quantile * 100))
    p("  options         %s" % ", ".join(sorted(v.options)))
    p("  flags           %s" % ", ".join(sorted(v.flags)))
    for h in v.hardware:
        p("  hardware        %s — %s" % (h.name, h.detail))
    if v.notes:
        p("  note            %s" % v.notes)


if __name__ == "__main__":
    for m in metrics():
        describe(m)
        print()
