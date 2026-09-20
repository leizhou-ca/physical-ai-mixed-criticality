#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The command line.
#
# `check` is the most valuable command here and the one a new user runs
# first: it answers "would a measurement taken here be valid?" without
# measuring, and it changes nothing. `explain` is the second: it says what a
# config will do and how long it will take, which is how someone catches a
# mistake before spending forty minutes measuring the wrong thing.
#
# THE PIPELINE IS COLLECT, DERIVE, ATTRIBUTE, REPORT, and `run` is all four.
# Each stage is also a command of its own, because a user debugging a port
# needs them separately and a user who never decomposes the pipeline should
# never have to.
#
# THE DRIVER IS THIN ON PURPOSE. It knows stage names, file paths, and which
# rule a victim declares — nothing about how any stage works. Interval
# construction lives in the rules; attribution lives in the analysis modules;
# the verdict classes live in exactly one file and it is not this one. The
# coupling between the collecting half and the analysing half is this file
# and it is deliberately the thinnest thing that can join them.
#
# A STAGE FAILURE STOPS THE PIPELINE AND SAYS WHICH STAGE. A half-finished
# pipeline must not look like a completed one: the failure names the stage,
# the reason, and what exists on disk up to that point.
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))

import config as config_mod                             # noqa: E402
import preconditions                                    # noqa: E402
import session as session_mod                           # noqa: E402
import victims                                          # noqa: E402
from adapters import LocalAdapter                       # noqa: E402

import mcib_attribute                                   # noqa: E402
import mcib_derive                                      # noqa: E402
import rules                                            # noqa: E402
from thresholds import Thresholds                       # noqa: E402

DERIVED_DIR = "derived"
VERDICTS_DIR = "verdicts"


class StageError(Exception):
    """A stage could not finish. Carries the stage's name so that the
    pipeline can say which one stopped it."""

    def __init__(self, stage, message):
        Exception.__init__(self, message)
        self.stage = stage
        self.message = message


def _thresholds(cfg):
    """The analysis thresholds this session runs under.

    Resolution order, stated in the config documentation and implemented
    here: the defaults in the analysis threshold module, overridden by the
    session config, recorded in every artefact. A name the analysis does not
    have is refused here rather than ignored — a threshold silently dropped
    is a session that ran under different numbers than the file asked for."""
    try:
        return Thresholds(overrides=cfg["thresholds"] or None)
    except KeyError as e:
        raise StageError("attribute",
                         "the config sets a threshold this build does not "
                         "have: %s. `python3 framework/analysis/thresholds.py`"
                         " lists every threshold and its default" % e)


def _load(path):
    try:
        return config_mod.load(path)
    except config_mod.ConfigError as e:
        e.render(sys.stderr)
        raise SystemExit(2)
    except IOError as e:
        print("cannot read %s: %s" % (path, e), file=sys.stderr)
        raise SystemExit(2)


def _capabilities(describe):
    lines = []
    t = describe["tracing"]
    if t["available"]:
        lines.append("kernel-event collection: available (%s, per-CPU ring "
                     "buffers; no global tracing state is taken)"
                     % t["source"])
    else:
        lines.append("kernel-event collection: UNAVAILABLE — %s" % t["note"])
    lines.append("privilege: %s"
                 % ("root" if describe["privileged"]
                    else "unprivileged; most preconditions cannot be fixed "
                         "from here"))
    lines.append("kernel: %s, %s"
                 % (describe["kernel_release"],
                    "PREEMPT_RT" if "PREEMPT_RT" in describe["kernel_version"]
                    else "no PREEMPT_RT"))
    return lines


def cmd_check(args):
    cfg = _load(args.config)
    ad = LocalAdapter()
    plan = session_mod.build_plan(cfg, ad)
    results = preconditions.check_all(ad, plan)
    print("mcib check — %s" % cfg["_source"])
    print("session `%s`: %s on cpu%d, %d run%s"
          % (cfg["session"], ", ".join(cfg["victim"]["metrics"]),
             cfg["victim"]["cpu"], config_mod.estimate(cfg)["runs"],
             "" if config_mod.estimate(cfg)["runs"] == 1 else "s"))
    print("")
    ok = preconditions.render(results, _capabilities(ad.describe()),
                              verbose=args.verbose)
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2,
                         sort_keys=True))
    return 0 if ok else 1


def cmd_explain(args):
    cfg = _load(args.config)
    est = config_mod.estimate(cfg)
    v, a = cfg["victim"], cfg["aggressors"]
    blocks = session_mod.run_order(cfg)

    print("%s will:" % cfg["_source"])
    print("")
    print("  measure   %s" % ", ".join(
        "%s (%s)" % (m, victims.get(m).description.split(" — ")[0])
        for m in v["metrics"] if m in victims.MANIFEST))
    print("  on        cpu%d, SCHED_FIFO priority %d, in domain `%s`"
          % (v["cpu"], v["priority"], v["domain"]))
    print("  under     %s" % ", ".join(a["conditions"]))
    print("            generated by %s on cpu%s, intensity %d"
          % (a["tool"], ",".join(str(c) for c in a["cpus"]), a["intensity"]))
    if "io" in a["conditions"]:
        fs = session_mod._filesystem_of(a["io_path"])
        if fs and fs.get("ram_backed"):
            print("  NOTE      the io aggressor writes to %s, which is %s — "
                  "a RAM" % (a["io_path"], fs["type"]))
            print("            filesystem. It will contend for memory, not "
                  "for a storage device.")
        elif fs:
            print("  io writes %s on %s (%s)"
                  % (a["io_path"], fs["mount_point"], fs["type"]))
    print("  counters  %s, set `%s`"
          % (" and ".join(cfg["counters"]["state"]), cfg["counters"]["set"]))
    print("  repeats   %d" % cfg["repeats"])
    print("")
    print("  That is %d runs of %d iterations, in %d blocks. Each block warms "
          "the board" % (est["runs"], v["iterations"], len(blocks)))
    print("  for %ds and then runs its conditions in rotation, so that drift "
          "over the" % session_mod.WARMUP_SECONDS)
    print("  session falls across the conditions rather than into one of "
          "them.")
    print("")
    for m, n in sorted(est["intervals_per_run"].items()):
        note = ""
        if victims.get(m).intervals_per_iteration != 1:
            note = ("  (%d per iteration — this metric is measured in both "
                    "directions)" % victims.get(m).intervals_per_iteration)
        print("  %-8s %d intervals per run%s" % (m, n, note))
    print("")
    print("  Expected duration   %s" % config_mod.human_duration(
        est["seconds"]))
    print("  Expected disk       %d MB of records" % est["megabytes"])
    print("  Output              %s" % cfg["output"])
    print("")
    if args.runs:
        print("  Run order:")
        for b in blocks:
            print("    warm-up, then: %s"
                  % ", ".join(r["tag"] for r in b["runs"]))
        print("")
    print("  Nothing has been measured. Run `mcib check %s` to find out "
          "whether" % cfg["_source"])
    print("  a measurement here would be valid.")
    return 0


def cmd_init(args):
    text = config_mod.init_text(args.journey)
    if args.output and args.output != "-":
        if os.path.exists(args.output) and not args.force:
            print("%s already exists; pass --force to overwrite"
                  % args.output, file=sys.stderr)
            return 2
        with open(args.output, "w") as f:
            f.write(text)
        print("wrote %s" % args.output)
        print("Next: mcib explain %s" % args.output)
    else:
        sys.stdout.write(text)
    return 0


def cmd_collect(args, announce_stage=True):
    cfg = _load(args.config)
    ad = LocalAdapter()
    plan = session_mod.build_plan(cfg, ad)

    results = preconditions.check_all(ad, plan)
    failures = [r for r in results if r.state == preconditions.FAIL]
    if failures and not args.force:
        print("mcib: preconditions are not met; nothing has been measured.")
        print("")
        preconditions.render(results, _capabilities(ad.describe()))
        print("")
        print("Re-run `mcib check %s` after fixing these, or pass --force to "
              "measure anyway" % cfg["_source"])
        print("(a forced run records the failures in its conditions and is "
              "not a valid result).")
        return 1
    if failures and args.force:
        print("mcib: FORCED — %d precondition%s not met. This session's "
              "conditions will record that." % (len(failures),
                                                "" if len(failures) == 1
                                                else "s"))

    os.makedirs(cfg["output"], exist_ok=True)
    with open(os.path.join(cfg["output"], "resolved-config.yaml"), "w") as f:
        f.write(config_mod.resolved_yaml(cfg))
    with open(os.path.join(cfg["output"], "preconditions.json"), "w") as f:
        json.dump([r.as_dict() for r in results], f, indent=2, sort_keys=True)
        f.write("\n")

    s = session_mod.Session(cfg, ad)
    outcomes = s.run()

    manifest = {
        "kind": "mcib.session",
        "version": 1,
        "session": cfg["session"],
        "config": config_mod.resolved_yaml(cfg),
        "runs": outcomes,
        "domain": ad.describe(),
    }
    with open(os.path.join(cfg["output"], "session.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
        f.write("\n")

    failed = [o for o in outcomes if o.get("failed")]
    drifted = [o for o in outcomes if o.get("drifted")]
    print("")
    print("%d runs, %d failed, %d with drifted conditions"
          % (len(outcomes), len(failed), len(drifted)))
    for o in failed:
        print("  FAILED %s: %s" % (o["tag"], o.get("error", "see .out")))
    for o in drifted:
        print("  DRIFT  %s: %s" % (o["tag"], ", ".join(
            "%s %s->%s" % (d["field"], d["before"], d["after"])
            for d in o["drift"])))
    print("")
    print("Records and conditions: %s" % cfg["output"])
    print("Resolved configuration: %s"
          % os.path.join(cfg["output"], "resolved-config.yaml"))
    if announce_stage:
        print("")
        print("Records only. `mcib derive %s` builds intervals from them, "
              "or `mcib run %s`" % (cfg["_source"], cfg["_source"]))
        print("runs the whole pipeline.")
    if failed:
        raise StageError("collect",
                         "%d of %d runs failed; the records for them do not "
                         "exist and nothing downstream can be computed from "
                         "them" % (len(failed), len(outcomes)))
    return 0


# ---------------------------------------------------------------- the stages
#
# Each one is a function of a config and the directory the previous stage
# wrote. None of them knows how the stage beneath it works: `derive` asks the
# victim manifest which rule a metric's events pair under and hands it to the
# rules module; `attribute` hands a directory of artefacts and a threshold
# set to the attribution driver; `report` reads what attribute wrote and
# prints it.

def stage_derive(cfg, log=print):
    """Records to intervals, one artefact per run.

    The driver knows which records a run produced and which rule the victim
    declares, both of which it asks the manifest. It does not know what
    either rule does."""
    out = cfg["output"]
    derived = os.path.join(out, DERIVED_DIR)
    written, skipped = [], []
    for block in session_mod.run_order(cfg):
        for spec in block["runs"]:
            metric = spec["metric"]
            victim = victims.get(metric)
            settings = session_mod.victim_settings(cfg, metric,
                                                   spec["counters"])
            base = os.path.join(out, spec["tag"] + ".csv")
            records = [path for _, path in victim.records_for(base, settings)]
            missing = [r for r in records if not os.path.exists(r)]
            if missing:
                raise StageError(
                    "derive",
                    "%s: the records this run should have written are not "
                    "there (%s). Intervals cannot be derived from a run that "
                    "did not produce a record"
                    % (spec["tag"], ", ".join(os.path.basename(m)
                                              for m in missing)))
            os.makedirs(derived, exist_ok=True)
            target = os.path.join(derived, spec["tag"] + ".json")
            if os.path.exists(target):
                skipped.append(target)
                continue
            art, ns = rules.apply_named(victim.rule, victim.rule_parameters,
                                        records, metric)
            mcib_derive.write_artefact(
                art, target, values=[int(round(v)) for v in ns])
            written.append(target)
    log("  %d artefacts written, %d already present" % (len(written),
                                                        len(skipped)))
    return derived


def stage_attribute(cfg, derived, log=print):
    """Intervals to verdicts. The thresholds come from the config and are
    recorded in every artefact written."""
    if not os.path.isdir(derived):
        raise StageError("attribute",
                         "there is no %s directory; derive has not run"
                         % derived)
    th = _thresholds(cfg)
    verdicts = os.path.join(cfg["output"], VERDICTS_DIR)
    try:
        artefacts, problems, index = mcib_attribute.attribute(
            derived, verdicts, thresholds=th, session=cfg["session"],
            root=cfg["output"], log=lambda *a: None)
    except mcib_attribute.AttributionError as e:
        raise StageError("attribute", str(e))
    log("  %d cells, %d artefacts not in any cell" % (len(artefacts),
                                                      len(problems)))
    return verdicts


def stage_report(cfg, verdicts, log=print):
    index = os.path.join(verdicts, "verdicts.json")
    if not os.path.exists(index):
        raise StageError("report",
                         "there is no %s; attribute has not run" % index)
    render_verdicts(json.load(open(index)), verdicts)
    return verdicts


# ------------------------------------------------------------- presentation
#
# This renders; it derives nothing. Every figure below was decided by the
# attribution stage and written into an artefact, and this reads it back.

def render_verdicts(index, verdicts_dir, stream=sys.stdout):
    p = lambda *a: print(*a, file=stream)
    rows = index["cells"]
    p("verdicts — session `%s`, from %s"
      % (index.get("session") or "unnamed", index["derived"]))
    p("")
    p("  %-8s %-5s %-8s %-8s %-12s %-22s %s"
      % ("metric", "arm", "counters", "aggressor", "verdict", "channel",
         "flags"))
    for r in rows:
        p("  %-8s %-5s %-8s %-8s %-12s %-22s %s"
          % (r["metric"], r["arm"], r["counter_state"], r["aggressor"],
             r["verdict"], (r["channel"] or "-")[:22],
             ", ".join(r["flags"]) or "-"))
    p("")
    classes = {}
    for r in rows:
        classes[r["verdict"]] = classes.get(r["verdict"], 0) + 1
    p("  %d cells: %s" % (len(rows), ", ".join(
        "%d %s" % (n, k) for k, n in sorted(classes.items()))))
    for problem in index.get("not_in_any_cell", []):
        p("  not in any cell: %s — %s" % (problem["artefact"],
                                          problem["reason"]))
    p("")
    p("  Every verdict states the statistic it rests on, the thresholds it "
      "was computed")
    p("  under, and every input by content hash. `%s/<cell>.verdict.json` "
      "has them." % verdicts_dir)


def cmd_derive(args):
    cfg = _load(args.config)
    print("derive — %s" % cfg["_source"])
    derived = stage_derive(cfg)
    print("Intervals: %s" % derived)
    return 0


def cmd_attribute(args):
    cfg = _load(args.config)
    print("attribute — %s" % cfg["_source"])
    verdicts = stage_attribute(cfg, os.path.join(cfg["output"], DERIVED_DIR))
    print("Verdicts: %s" % verdicts)
    print("")
    stage_report(cfg, verdicts)
    return 0


def cmd_report(args):
    cfg = _load(args.config)
    return 0 if stage_report(
        cfg, os.path.join(cfg["output"], VERDICTS_DIR)) else 1


def cmd_run(args):
    """The whole pipeline: collect, derive, attribute, report.

    A stage that fails stops the pipeline and says which stage and why. What
    the earlier stages wrote stays on disk and is named, because a partial
    result that can be resumed is worth more than a clean slate — but it is
    never presented as a completed run."""
    cfg = _load(args.config)
    stages = ["collect", "derive", "attribute", "report"]
    done = []
    try:
        print("=== collect")
        if cmd_collect(args, announce_stage=False) != 0:
            raise StageError("collect",
                             "preconditions are not met, so nothing was "
                             "measured. Every stage below this one would be "
                             "computing from records that do not exist")
        done.append("collect")
        print("")
        print("=== derive")
        derived = stage_derive(cfg)
        done.append("derive")
        print("")
        print("=== attribute")
        verdicts = stage_attribute(cfg, derived)
        done.append("attribute")
        print("")
        print("=== report")
        stage_report(cfg, verdicts)
        done.append("report")
    except StageError as e:
        print("")
        print("mcib run: STOPPED IN STAGE `%s`" % e.stage, file=sys.stderr)
        print("  %s" % e.message, file=sys.stderr)
        print("", file=sys.stderr)
        print("  completed: %s" % (", ".join(done) or "nothing"),
              file=sys.stderr)
        print("  not run:   %s"
              % ", ".join(x for x in stages if x not in done
                          and x != e.stage), file=sys.stderr)
        print("  output so far: %s" % cfg["output"], file=sys.stderr)
        print("", file=sys.stderr)
        print("  This is not a completed run and its output is not a result.",
              file=sys.stderr)
        return 1
    return 0


def cmd_victims(args):
    for m in victims.metrics():
        victims.describe(m)
        print("")
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="mcib",
        description="sequence and collect mixed-criticality interference "
                    "measurements")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("check", help="preconditions only; changes nothing")
    p.add_argument("config")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("explain", help="what this config will do, and for "
                                       "how long")
    p.add_argument("config")
    p.add_argument("--runs", action="store_true",
                   help="also list every run in order")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("init", help="write a commented, working config")
    p.add_argument("journey", choices=sorted(config_mod.JOURNEYS))
    p.add_argument("-o", "--output", default="-")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("collect", help="check, then measure; stop after "
                                       "records")
    p.add_argument("config")
    p.add_argument("--force", action="store_true",
                   help="measure even though preconditions failed; the "
                        "result is not valid and says so")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("derive", help="records -> intervals")
    p.add_argument("config")
    p.set_defaults(fn=cmd_derive)

    p = sub.add_parser("attribute", help="intervals -> verdicts")
    p.add_argument("config")
    p.set_defaults(fn=cmd_attribute)

    p = sub.add_parser("report", help="verdicts -> a table, for humans")
    p.add_argument("config")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("run", help="the whole pipeline: collect, derive, "
                                   "attribute, report")
    p.add_argument("config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("victims", help="the victim manifest")
    p.set_defaults(fn=cmd_victims)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        return args.fn(args)
    except StageError as e:
        print("mcib %s: %s" % (e.stage, e.message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
