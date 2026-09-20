#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The run configuration: one file describes one session.
#
# This is the first artefact a third party writes and the only one they must
# understand, so the work here is mostly diagnostics. Three things follow
# from that:
#
# EVERY ERROR CARRIES A LINE NUMBER AND A SUGGESTION. A schema error that
# says only "unknown key" leaves a user comparing their file to an example
# character by character. Keys are therefore tracked back to the line they
# were written on, and an unrecognised one is matched against what would have
# been valid there.
#
# INDENTATION GETS THE MOST HELP, because it is YAML's hazard for someone who
# has not used it before and because the failure is silent: a key indented
# one level too far is not a syntax error, it is a different setting that
# happens not to exist. Where an unknown key would have been valid somewhere
# else in the file, the message says so.
#
# NOTHING TOUCHES THE BOARD UNTIL THE FILE IS VALID. A config error found
# after the warm-up has cost a minute of the user's time; found after the
# session, forty.
#
# DEFAULTS EVERYWHERE, AND EVERY DEFAULT PRINTED. The smallest valid file is
# four lines. What it resolved to is written into the run's output, so a user
# can read back the whole session — including everything they did not write —
# and use it as their next starting point. A default that is not recorded is
# a default that silently changes between versions.
#
# THE SCHEMA IS MULTI-DOMAIN FROM THE FIRST VERSION even though only one
# domain can be used. A single-domain format extended later is two
# incompatible formats, and the users who wrote the first one are the ones
# who pay for that.
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import victims                                          # noqa: E402

try:
    import yaml
except ImportError:                                      # pragma: no cover
    yaml = None

# Estimation constants. They exist to answer "how long will this take?"
# before a user spends the time, and they are deliberately approximate: the
# figure that matters is the order of magnitude, so that forty minutes is not
# mistaken for four. Each is a rate this campaign observed on its own board.
SECONDS_PER_MILLION_ITERATIONS = {"imlat": 25.0, "tslat": 40.0,
                                  "prelat": 30.0, "inlat": 120.0}
DEFAULT_SECONDS_PER_MILLION = 40.0
WARMUP_SECONDS = 60
PER_RUN_OVERHEAD_SECONDS = 12      # stressor start, settle, stop, conditions
BYTES_PER_EVENT = 73               # measured across this campaign's records
EVENTS_PER_ITERATION = {"imlat": 2, "tslat": 4, "prelat": 2, "inlat": 2}

CONDITIONS = ("none", "cpu", "memory", "io", "mixed")

# The established tag vocabulary the records and artefacts already use. The
# config speaks the reader's language; the filesystem keeps the names the
# existing dataset was written with, so a session can be compared against one
# recorded before this tool existed.
CONDITION_TAG = {"none": "baseline", "cpu": "cpu", "memory": "mem",
                 "io": "io", "mixed": "mixed"}


class ConfigError(Exception):
    """A list of problems, not one. A user fixing a config one error per run
    is the same user fixing preconditions one abort at a time."""

    def __init__(self, problems, path=None):
        self.problems = problems
        self.path = path
        Exception.__init__(self, "%d problem%s in %s"
                           % (len(problems), "" if len(problems) == 1 else "s",
                              path or "config"))

    def render(self, stream=None):
        stream = stream or sys.stderr
        print("%s: %d problem%s" % (self.path or "config", len(self.problems),
                                    "" if len(self.problems) == 1 else "s"),
              file=stream)
        print("", file=stream)
        for p in self.problems:
            where = ("line %d" % p["line"]) if p.get("line") else "file"
            print("  %s: %s" % (where, p["message"]), file=stream)
            if p.get("hint"):
                print("      %s" % p["hint"], file=stream)
            print("", file=stream)


# ------------------------------------------------- YAML, with line numbers

def _load_with_lines(text, path):
    """Parse YAML, recording the line each key was written on.

    A third-party parser is used rather than a hand-written one. A subtly
    wrong YAML parser does not fail — it reads a different configuration than
    the user wrote, and the run that follows is valid, reproducible and
    measuring the wrong thing.

    Line tracking is added by walking the node tree the parser composes,
    rather than by hooking the construction of values. The constructor builds
    a nested mapping in two passes and the object it returns for a nested
    block is not the object that ends up in the result, so line numbers keyed
    by object identity attach to dictionaries nobody ever sees. Keying them
    by their path through the document is exact and does not depend on how
    the parser happens to allocate."""
    if yaml is None:
        raise ConfigError([{
            "line": None,
            "message": "PyYAML is not installed, and the configuration "
                       "cannot be read without it",
            "hint": "pip install pyyaml  (this is the orchestrator's only "
                    "dependency outside the standard library)",
        }], path)

    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise ConfigError([{
            "line": (mark.line + 1) if mark else None,
            "message": "this file is not valid YAML: %s"
                       % (getattr(e, "problem", None) or str(e)),
            "hint": "YAML is indentation-sensitive: use spaces, never tabs, "
                    "and keep every key in a block at the same indentation.",
        }], path)

    lines = {}

    def build(n, where):
        if isinstance(n, yaml.MappingNode):
            out = {}
            for key_node, value_node in n.value:
                key = str(key_node.value)
                lines[where + (key,)] = key_node.start_mark.line + 1
                out[key] = build(value_node, where + (key,))
            return out
        if isinstance(n, yaml.SequenceNode):
            return [build(c, where) for c in n.value]
        return loader.construct_object(n, deep=True)

    try:
        data = build(node, ()) if node is not None else {}
    finally:
        loader.dispose()
    return (data if data is not None else {}), lines


def _line_of(lines, *path):
    """The line a key was written on, addressed by its path in the file."""
    return lines.get(tuple(p for p in path if p is not None))


# ------------------------------------------------------------- the schema
#
# Declared as data so that the validator, the suggestion machinery and the
# `init` template all read the same description, and so that a key added in
# one of them cannot go missing from the other two.

SCHEMA = {
    "session": {"type": str, "default": "session",
                "doc": "a name for this session; it is written into every "
                       "record and artefact it produces"},
    "domains": {"type": dict, "default": {"local": {"adapter": "local"}},
                "doc": "where roles execute. One entry per domain; the "
                       "single-domain case is one entry with the local "
                       "adapter"},
    "origin": {"type": list, "default": [],
               "doc": "clock relationships between domains. Absent means "
                      "single-domain; an unestablished origin makes a "
                      "cross-domain interval refused, never estimated"},
    "victim": {"type": dict, "default": {}, "doc": "what is measured", "keys": {
        "metrics": {"type": list, "default": ["imlat"],
                    "doc": "which metrics to measure, from the manifest"},
        "metric": {"type": str, "default": None,
                   "doc": "a single metric, as an alternative to `metrics`"},
        "domain": {"type": str, "default": "local",
                   "doc": "which domain the victim runs in"},
        "cpu": {"type": int, "default": 3,
                "doc": "the measured core; it must be isolated"},
        "priority": {"type": int, "default": 95,
                     "doc": "SCHED_FIFO priority of the measuring context"},
        "priority_gap": {"type": int, "default": None,
                         "doc": "how far below the waiter the trigger runs; "
                                "only the metrics that have two priorities"},
        "iterations": {"type": int, "default": 100000,
                       "doc": "iterations per run. Note this is not the "
                              "interval count for every metric"},
        "warmup": {"type": int, "default": None,
                   "doc": "warm-up events discarded before measuring"},
        "counter_backend": {"type": str, "default": None,
                            "doc": "counter backend; only the metrics that "
                                   "accept one"},
        "boundary_timestamps": {"type": bool, "default": None,
                                "doc": "record kernel timestamps at the "
                                       "boundary; one metric only"},
        "instrument_server": {"type": bool, "default": None,
                              "doc": "instrument the echo server as a second "
                                     "context; one metric only"},
        "counters_in_server": {"type": bool, "default": None,
                               "doc": "counters in the second context; needs "
                                      "instrument_server"},
        "atomic_mode": {"type": int, "default": None,
                        "doc": "characterisation mode; one metric only"},
        "relaxed_scheduling": {"type": bool, "default": None,
                               "doc": "tolerate the absence of real-time "
                                      "scheduling. Never use for a result"},
    }},
    "aggressors": {"type": dict, "default": {}, "doc": "the interference",
                   "keys": {
        "domain": {"type": str, "default": "local",
                   "doc": "which domain the aggressors run in"},
        "cpus": {"type": list, "default": [0, 1],
                 "doc": "the cores the aggressors are confined to"},
        "conditions": {"type": list, "default": ["none"],
                       "doc": "which conditions to run: %s"
                              % ", ".join(CONDITIONS)},
        "tool": {"type": str, "default": "stress-ng",
                 "doc": "the load generator. The framework does not generate "
                        "load itself"},
        "intensity": {"type": int, "default": 1,
                      "doc": "1 is the established single-aggressor "
                             "intensity; 2 doubles the worker count"},
        "io_path": {"type": str, "default": "/tmp/stress-io",
                    "doc": "where the I/O aggressor writes. THIS DECIDES "
                           "WHAT THE AGGRESSOR IS: on many systems /tmp is a "
                           "RAM filesystem, and an I/O aggressor pointed at "
                           "RAM contends for memory rather than for storage. "
                           "The default matches the path this campaign's "
                           "recorded runs used; the filesystem it resolves "
                           "to is recorded with every run"},
        "io_bytes": {"type": str, "default": "64M",
                     "doc": "cap on the I/O aggressor's writes. Uncapped it "
                            "fills the filesystem, and capped against "
                            "uncapped reversed an aggressor ranking here"},
    }},
    "counters": {"type": dict, "default": {}, "doc": "counter configuration",
                 "keys": {
        "set": {"type": str, "default": "armv3-default",
                "doc": "the named counter set the victim opens"},
        "state": {"type": list, "default": ["on"],
                  "doc": "run with counters on, off, or both. Counter state "
                         "changed this campaign's numbers by 2-3x and "
                         "reordered which metric was worst"},
    }},
    "repeats": {"type": int, "default": 5,
                "doc": "repeats per cell. The spread across them is the "
                       "noise floor a shift is judged against, so fewer than "
                       "two leaves nothing to judge against"},
    "repeat_from": {"type": int, "default": 1,
                    "doc": "the first repeat number this invocation runs. "
                           "Present because a board whose card holds one "
                           "block cannot hold a whole campaign: the repeats "
                           "are run as separate invocations, and the records "
                           "are collected off the target and cleared between "
                           "them. It changes WHICH repeats run and nothing "
                           "about what a run is — every other setting, the "
                           "warm-up and the rotation of conditions inside a "
                           "block are unchanged, so repeat 3 taken this way "
                           "is the same experiment as repeat 3 taken in one "
                           "long session"},
    "output": {"type": str, "default": "./results/",
               "doc": "where records, captures and conditions are written"},
    "deploy": {"type": dict, "default": {}, "doc": "where binaries live",
               "keys": {
        "path": {"type": str, "default": ".",
                 "doc": "directory holding the victim binaries in the "
                        "victim's domain"},
    }},
    "collectors": {"type": list, "default": [],
                   "doc": "kernel-event collection. Per-CPU ring buffers "
                          "started and stopped with the run; the "
                          "orchestrator owns no global tracing state"},
    "thresholds": {"type": dict, "default": {},
                   "doc": "analysis threshold overrides. Resolution order: "
                          "defaults in the analysis thresholds module, "
                          "overridden by this block, recorded in the "
                          "artefact"},
    "preconditions": {"type": dict, "default": {},
                      "doc": "the values preconditions are checked against",
                      "keys": {
        "governor": {"type": str, "default": "performance",
                     "doc": "the required CPU frequency governor"},
        "rt_runtime_us": {"type": int, "default": -1,
                          "doc": "sched_rt_runtime_us; -1 disables real-time "
                                 "throttling"},
        "load_ceiling": {"type": float, "default": 1.0,
                         "doc": "1-minute load average allowed before the "
                                "first run"},
        "free_mb_floor": {"type": int, "default": 120,
                          "doc": "free megabytes required before each run"},
        "perf_event_paranoid_max": {"type": int, "default": 1,
                                    "doc": "the highest paranoid setting "
                                           "counters are expected to open at"},
    }},
}


def _all_key_paths():
    out = {}
    for key, spec in SCHEMA.items():
        out.setdefault(key, []).append("(top level)")
        for sub in (spec.get("keys") or {}):
            out.setdefault(sub, []).append(key)
    return out


def _suggest(key, valid, problems_line, section, all_paths):
    """The nearest valid alternative, and the indentation hint.

    The indentation case is checked FIRST because it is both the most common
    mistake and the one a spelling suggestion answers unhelpfully: telling
    someone that `cpus` looks like `cpu` is worse than useless when what they
    did was indent an aggressor setting under the victim."""
    elsewhere = [s for s in all_paths.get(key, []) if s != section]
    if elsewhere:
        where = elsewhere[0]
        return ("`%s` is a valid setting, but not under `%s`. It belongs "
                "under `%s` — check the indentation on this line."
                % (key, section, where)
                if where != "(top level)" else
                "`%s` is a valid top-level setting, but it is indented under "
                "`%s`. Move it to the start of a line." % (key, section))
    close = difflib.get_close_matches(key, sorted(valid), n=2, cutoff=0.6)
    if close:
        return "did you mean %s?" % " or ".join("`%s`" % c for c in close)
    return "valid settings here: %s" % ", ".join(sorted(valid))


def _type_name(t):
    return {str: "text", int: "a whole number", float: "a number",
            bool: "true or false", list: "a list", dict: "a block"}.get(
                t, getattr(t, "__name__", str(t)))


def _check_type(value, want):
    if want is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if want is bool:
        return isinstance(value, bool)
    return isinstance(value, want)


# ---------------------------------------------------------------- validate

def load(path):
    with open(path) as f:
        text = f.read()
    data, lines = _load_with_lines(text, path)
    problems = []
    all_paths = _all_key_paths()

    if not isinstance(data, dict):
        raise ConfigError([{
            "line": 1,
            "message": "the file must be a block of settings",
            "hint": "the smallest valid file is:\n"
                    "          victim: { metrics: [imlat] }\n"
                    "          output: ./results/",
        }], path)

    # unknown and mistyped keys, top level and one level down
    for key in list(data):
        if key not in SCHEMA:
            problems.append({
                "line": _line_of(lines, key),
                "path": (key,),
                "message": "unknown setting `%s`" % key,
                "hint": _suggest(key, SCHEMA.keys(), None, "(top level)",
                                 all_paths),
            })
            continue
        spec = SCHEMA[key]
        value = data[key]
        if value is not None and not _check_type(value, spec["type"]):
            problems.append({
                "line": _line_of(lines, key),
                "path": (key,),
                "message": "`%s` should be %s, but this is %s"
                           % (key, _type_name(spec["type"]),
                              _type_name(type(value))),
                "hint": spec["doc"],
            })
            continue
        sub_schema = spec.get("keys")
        if sub_schema and isinstance(value, dict):
            for sub in list(value):
                if sub not in sub_schema:
                    problems.append({
                        "line": _line_of(lines, key, sub),
                        "path": (key, sub),
                        "message": "unknown setting `%s` under `%s`"
                                   % (sub, key),
                        "hint": _suggest(sub, sub_schema.keys(), None, key,
                                         all_paths),
                    })
                    continue
                sv = value[sub]
                if sv is not None and not _check_type(sv, sub_schema[sub]["type"]):
                    problems.append({
                        "line": _line_of(lines, key, sub),
                        "path": (key, sub),
                        "message": "`%s` should be %s, but this is %s"
                                   % (sub, _type_name(sub_schema[sub]["type"]),
                                      _type_name(type(sv))),
                        "hint": sub_schema[sub]["doc"],
                    })

    # The semantic checks run even when the schema pass found something, on
    # a copy with the offending entries removed. A user who has both a
    # mistyped key and a metric this build does not have should learn about
    # both now — fixing one error per invocation is how a user gives up.
    resolved = _resolve(_without(data, problems))
    problems += _validate_semantics(resolved, lines)
    if problems:
        raise ConfigError(_ordered(problems), path)
    resolved["_source"] = path
    return resolved


def _without(data, problems):
    """A copy of the parsed file with everything the schema pass rejected
    taken out, so that resolution and the semantic checks can proceed far
    enough to find whatever else is wrong."""
    bad_top = set()
    bad_sub = {}
    for p in problems:
        path = p.get("path")
        if not path:
            continue
        if len(path) == 1:
            bad_top.add(path[0])
        elif len(path) == 2:
            bad_sub.setdefault(path[0], set()).add(path[1])
    out = {}
    for k, v in data.items():
        if k in bad_top:
            continue
        if k in bad_sub and isinstance(v, dict):
            v = dict((sk, sv) for sk, sv in v.items()
                     if sk not in bad_sub[k])
        out[k] = v
    return out


def _ordered(problems):
    """By line, so the list reads in the order the file does. Problems with
    no line — a whole-file matter — come last."""
    return sorted(problems, key=lambda p: (p.get("line") is None,
                                           p.get("line") or 0))


def _resolve(data):
    """Fill in every default, so that what runs is fully stated."""
    out = {}
    for key, spec in SCHEMA.items():
        value = data.get(key, None)
        if spec.get("keys"):
            block = dict((k, v["default"]) for k, v in spec["keys"].items())
            block.update(value or {})
            out[key] = block
        elif value is None:
            default = spec["default"]
            out[key] = dict(default) if isinstance(default, dict) else (
                list(default) if isinstance(default, list) else default)
        else:
            out[key] = value
    # YAML reads a bare `on` and `off` as booleans, so `state: [on, off]` —
    # the spelling the documentation shows, and the natural one — arrives
    # here as [True, False]. Normalising is not indulgence: the alternative
    # is telling every user to quote two particular words, and the error they
    # get when they forget reads "counter state `True` is neither on nor
    # off", which is baffling when the file plainly says `on`.
    state = out["counters"].get("state")
    if isinstance(state, list):
        out["counters"]["state"] = [_counter_state(x) for x in state]

    # `metric` is a convenience for `metrics`; one list downstream.
    v = out["victim"]
    if v.get("metric"):
        v["metrics"] = [v["metric"]]
    v.pop("metric", None)
    return out


def _counter_state(value):
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


def _validate_semantics(r, lines):
    problems = []

    # metrics must be in the manifest
    for m in r["victim"]["metrics"]:
        if m not in victims.MANIFEST:
            problems.append({
                "line": _line_of(lines, "victim", "metrics")
                        or _line_of(lines, "victim", "metric"),
                "message": "`%s` is not a metric this build knows" % m,
                "hint": "available: %s%s" % (
                    ", ".join(victims.metrics()),
                    ("; did you mean `%s`?" % difflib.get_close_matches(
                        m, victims.metrics(), n=1, cutoff=0.5)[0])
                    if difflib.get_close_matches(m, victims.metrics(), n=1,
                                                 cutoff=0.5) else ""),
            })

    # a victim setting the chosen metric does not accept
    per_victim = ("counter_backend", "boundary_timestamps",
                  "instrument_server", "counters_in_server", "atomic_mode",
                  "priority_gap")
    for setting in per_victim:
        if r["victim"].get(setting) in (None, False):
            continue
        for m in r["victim"]["metrics"]:
            if m in victims.MANIFEST and not victims.get(m).supports(setting):
                supporting = victims.victims_supporting(setting)
                problems.append({
                    "line": _line_of(lines, "victim", setting),
                    "message": "`%s` is set, but the metric `%s` does not "
                               "accept it" % (setting, m),
                    "hint": "only %s accept%s `%s`. Remove it, or run %s in "
                            "a separate session."
                            % (" and ".join(supporting),
                               "" if len(supporting) > 1 else "s",
                               setting, m),
                })

    if r["victim"].get("counters_in_server") and \
            not r["victim"].get("instrument_server"):
        problems.append({
            "line": _line_of(lines, "victim", "counters_in_server"),
            "message": "`counters_in_server` needs `instrument_server`",
            "hint": "counters in the second context require that context to "
                    "be instrumented at all. Set instrument_server: true.",
        })

    # domains referenced must be defined
    defined = set(r["domains"] or {})
    for block, key in (("victim", "domain"), ("aggressors", "domain")):
        dom = r[block].get(key)
        if dom and dom not in defined:
            problems.append({
                "line": _line_of(lines, block, key),
                "message": "`%s` refers to the domain `%s`, which is not "
                           "defined" % (block, dom),
                "hint": "defined domains: %s. Add it under `domains:`, or "
                        "use one of those." % (", ".join(sorted(defined))
                                               or "none"),
            })
    for i, entry in enumerate(r["origin"] or []):
        for dom in (entry or {}).get("between", []):
            if dom not in defined:
                problems.append({
                    "line": None,
                    "message": "origin entry %d refers to the domain `%s`, "
                               "which is not defined" % (i + 1, dom),
                    "hint": "defined domains: %s" % ", ".join(sorted(defined)),
                })

    # conditions
    for c in r["aggressors"]["conditions"]:
        if c not in CONDITIONS:
            close = difflib.get_close_matches(str(c), CONDITIONS, n=1,
                                              cutoff=0.4)
            problems.append({
                "line": _line_of(lines, "aggressors", "conditions"),
                "message": "`%s` is not a condition" % c,
                "hint": "conditions are: %s%s" % (
                    ", ".join(CONDITIONS),
                    "; did you mean `%s`?" % close[0] if close else ""),
            })

    # counter state
    for s in r["counters"]["state"]:
        if s not in ("on", "off"):
            problems.append({
                "line": _line_of(lines, "counters", "state"),
                "message": "counter state `%s` is neither on nor off" % s,
                "hint": "state: [on], [off], or [on, off] to run both",
            })

    if r["repeats"] < 1:
        problems.append({
            "line": _line_of(lines, "repeats"),
            "message": "`repeats` must be at least 1",
            "hint": "the spread across repeats is the noise floor a shift is "
                    "judged against; with fewer than two there is none",
        })

    # single-domain only, for now
    if len(defined) > 1:
        problems.append({
            "line": _line_of(lines, "domains"),
            "message": "this build runs single-domain sessions only, and "
                       "this config defines %d domains" % len(defined),
            "hint": "the schema is multi-domain so that a config written now "
                    "does not have to change later, but the remote adapters "
                    "are not built. Use one domain with the local adapter.",
        })
    for name, spec in (r["domains"] or {}).items():
        adapter = (spec or {}).get("adapter")
        if adapter != "local":
            problems.append({
                "line": _line_of(lines, "domains", name),
                "message": "domain `%s` uses the `%s` adapter, which this "
                           "build does not have" % (name, adapter),
                "hint": "only the `local` adapter ships today.",
            })
    return problems


# ------------------------------------------------------------- the estimate

def estimate(r):
    """How long, and how much space. Approximate by design — the number that
    matters is whether this is four minutes or forty."""
    metrics = r["victim"]["metrics"]
    n_cond = len(r["aggressors"]["conditions"])
    n_state = len(r["counters"]["state"])
    # The repeats this invocation runs, which is not always all of them:
    # a campaign split across invocations to fit a small card runs a slice.
    repeats = r["repeats"] - r.get("repeat_from", 1) + 1
    iters = r["victim"]["iterations"]

    runs = len(metrics) * n_cond * n_state * repeats
    seconds = 0.0
    megabytes = 0.0
    for m in metrics:
        rate = SECONDS_PER_MILLION_ITERATIONS.get(m, DEFAULT_SECONDS_PER_MILLION)
        per_run = iters / 1e6 * rate + PER_RUN_OVERHEAD_SECONDS
        n = n_cond * n_state * repeats
        seconds += per_run * n
        events = iters * EVENTS_PER_ITERATION.get(m, 2)
        megabytes += events * BYTES_PER_EVENT / 1e6 * n
    # one warm-up per condition block, per metric, per counter state, per repeat
    seconds += WARMUP_SECONDS * len(metrics) * n_state * repeats
    return {
        "runs": runs,
        "seconds": seconds,
        "megabytes": megabytes,
        "intervals_per_run": dict(
            (m, victims.get(m).intervals_for(iters))
            for m in metrics if m in victims.MANIFEST),
    }


def human_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, s)
    return "%d s" % s


# ------------------------------------------------------- printing and init

def render_resolved(r, stream=None):
    """The fully resolved configuration, including every default.

    Written into the run's output so a user can copy it back as their next
    starting point, and so that a result can be read years later without
    knowing what this version's defaults were."""
    import json
    stream = stream or sys.stdout
    out = dict((k, v) for k, v in r.items() if not k.startswith("_"))
    print(json.dumps(out, indent=2, sort_keys=True), file=stream)


def resolved_yaml(r):
    lines = ["# Fully resolved configuration, including every default.",
             "# This is what ran. It is a valid config: copy it back as a "
             "starting point.", ""]

    def emit(key, value, indent=0):
        pad = "  " * indent
        if isinstance(value, dict):
            if not value:
                lines.append("%s%s: {}" % (pad, key))
                return
            lines.append("%s%s:" % (pad, key))
            for k in sorted(value):
                emit(k, value[k], indent + 1)
        elif isinstance(value, list):
            if all(not isinstance(x, (dict, list)) for x in value):
                # `on` and `off` are quoted because YAML would read them back
                # as booleans; the point of this file is that it can be fed
                # straight back in.
                items = ", ".join(
                    ("'%s'" % x if str(x) in ("on", "off", "yes", "no")
                     else str(x)) for x in value)
                lines.append("%s%s: [%s]" % (pad, key, items))
            else:
                lines.append("%s%s:" % (pad, key))
                for x in value:
                    lines.append("%s  - %s" % (pad, x))
        elif value is None:
            lines.append("%s%s: null" % (pad, key))
        elif isinstance(value, bool):
            lines.append("%s%s: %s" % (pad, key, "true" if value else "false"))
        else:
            lines.append("%s%s: %s" % (pad, key, value))

    for key in sorted(k for k in r if not k.startswith("_")):
        emit(key, r[key])
    return "\n".join(lines) + "\n"


JOURNEYS = {
    "survey": {
        "title": "Is my platform any good?",
        "metrics": ["imlat", "tslat", "prelat"],
        "conditions": ["none", "cpu", "memory", "io", "mixed"],
        "state": ["on", "off"],
        "repeats": 5,
    },
    "quick": {
        "title": "a first run, to see the pipeline work end to end",
        "metrics": ["imlat"],
        "conditions": ["none", "memory"],
        "state": ["on"],
        "repeats": 2,
    },
}


def init_text(journey):
    """A commented, working config with the optional settings present and
    commented out. Nobody should start from a blank file — and a user who
    starts from one does not discover the settings that matter until a result
    is already wrong."""
    j = JOURNEYS[journey]
    out = []
    a = out.append
    a("# mcib session — %s" % j["title"])
    a("# Generated by: mcib init %s" % journey)
    a("#")
    a("# Everything not written here has a default, and every default is")
    a("# recorded in the run's output. The commented settings below are the")
    a("# ones worth knowing about before you trust a number.")
    a("")
    a("session: %s" % journey)
    a("")
    a("domains:")
    a("  local: { adapter: local }      # the machine this runs on")
    a("")
    a("victim:")
    a("  metrics: [%s]" % ", ".join(j["metrics"]))
    a("  cpu: 3                         # must be isolated — mcib check")
    a("  iterations: 100000")
    a("  # priority: 95                 # SCHED_FIFO priority")
    a("  # warmup: 2000                 # events discarded before measuring")
    a("  # counter_backend: linux-perf-percpu   # imlat and tslat only")
    a("")
    a("aggressors:")
    a("  cpus: [0, 1]                   # confined here, verified per thread")
    a("  conditions: [%s]" % ", ".join(j["conditions"]))
    a("  # intensity: 1                 # 2 doubles the worker count")
    a("  # io_bytes: 64M                # cap the I/O aggressor's writes")
    a("")
    a("counters:")
    a("  state: [%s]" % ", ".join(j["state"]))
    a("  # set: armv3-default")
    a("")
    a("repeats: %d                       # the spread across these is the"
      % j["repeats"])
    a("                                 # noise floor a shift is judged against")
    a("")
    a("output: ./results/%s/" % journey)
    a("")
    a("# deploy:")
    a("#   path: .                      # where the victim binaries are")
    a("")
    a("# thresholds:                    # analysis threshold overrides.")
    a("#   outlier_quantile: 0.99       # Resolution order: analysis defaults,")
    a("#                                # then this block, then recorded in")
    a("#                                # the artefact.")
    return "\n".join(out) + "\n"
