#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Sequencing one session.
#
# Every rule here was a line in a shell script before it was a rule here, and
# each exists because of something that went wrong without it.
#
# CONDITIONS ARE INTERLEAVED, NOT BLOCKED. Five consecutive runs of one
# condition followed by five of the next confounds the condition with
# everything that drifts over twenty minutes — the board's temperature, the
# filesystem filling, whatever else the machine decided to do. Running the
# conditions in rotation inside each repeat puts that drift across all of
# them instead of into one.
#
# WARM-UP IS PER CONDITION BLOCK, NOT PER RUN. A cold board and a hot board
# are different machines, and the first run after an idle period is measuring
# the transition. One warm-up before each rotation costs a minute; one before
# every run would cost more than the measurements.
#
# THE AGGRESSOR IS VERIFIED PER THREAD, AND ITS DEATH IS CONFIRMED. Confining
# the parent process does not confine children it has already forked, so
# every thread's allowed-CPU list is read back. And a load generator that
# outlives its run contaminates the next one — which is the baseline — so
# termination is confirmed before anything else starts.
#
# A RECORD THAT FAILS ITS OWN PRECONDITIONS FAILS THE RUN. The victim checks
# its own faults, its own scheduling and its own buffer, and says so in the
# record. Where it reports a problem the run is failed and re-run. Nothing
# repairs a record: a record is written once, and a repaired record is a
# measurement nobody can reconstruct.
#
# CONDITIONS ARE CAPTURED BEFORE AND AFTER EVERY RUN, structured, and written
# where the record names them. See conditions.py for why that is a contract
# and not a log.
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conditions as conditions_mod                     # noqa: E402
import preconditions                                    # noqa: E402
import victims                                          # noqa: E402
from config import CONDITION_TAG, estimate              # noqa: E402

STRESSOR_CGROUP = "/sys/fs/cgroup/stressor"
SETTLE_SECONDS = 1
TERMINATION_GRACE_SECONDS = 3
WARMUP_SECONDS = 60


# The aggressors, exactly as this campaign ran them. The parameters are part
# of the measurement: a capped and an uncapped I/O aggressor reversed an
# aggressor ranking here, so they are declared rather than left to a default
# that could change with the tool's version.
def aggressor_command(condition, intensity, io_bytes, io_tmp):
    if condition == "cpu":
        workers = 6 if intensity == 2 else 3
        return [["stress-ng", "--cpu", str(workers),
                 "--cpu-method", "matrixprod"]]
    if condition == "memory":
        workers = 4 if intensity == 2 else 2
        return [["stress-ng", "--vm", str(workers), "--vm-bytes", "512M",
                 "--vm-method", "walk-1d"]]
    if condition == "io":
        io_workers = 4 if intensity == 2 else 2
        hdd = 2 if intensity == 2 else 1
        return [["stress-ng", "--io", str(io_workers), "--hdd", str(hdd),
                 "--hdd-bytes", io_bytes, "--temp-path", io_tmp]]
    if condition == "mixed":
        # cpu AND memory concurrently, each at its single-aggressor
        # intensity, in the same confined cpuset. I/O is deliberately not
        # part of it: it writes to the filesystem, so stacking it would
        # change the storage's state between runs rather than the CPU's.
        return (aggressor_command("cpu", intensity, io_bytes, io_tmp)
                + aggressor_command("memory", intensity, io_bytes, io_tmp))
    return []


class SessionError(Exception):
    pass


def _filesystem_of(path):
    """Which filesystem a path lands on, and whether it is backed by RAM.

    Recorded because it decides what an I/O aggressor actually is. Pointed at
    a RAM filesystem — which /tmp very often is — an "I/O" aggressor never
    touches a storage device and contends for memory bandwidth and page cache
    instead. Two runs with the same declared condition and different answers
    here are not the same experiment, and nothing else in the record would
    show it."""
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    best, fstype, source = "", None, None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src, point, kind = parts[0], parts[1], parts[2]
                if (probe == point or probe.startswith(point.rstrip("/") + "/")) \
                        and len(point) > len(best):
                    best, fstype, source = point, kind, src
    except IOError:
        return None
    return {"path": path, "mount_point": best, "type": fstype,
            "source": source,
            "ram_backed": fstype in ("tmpfs", "ramfs"),
            "note": ("this is a RAM filesystem: an I/O aggressor writing "
                     "here contends for memory, not for a storage device"
                     if fstype in ("tmpfs", "ramfs") else None)}


def build_plan(cfg, adapter):
    """The facts the preconditions and the conditions capture both need.

    One structure, built once, so that `check` and `run` are asking the same
    questions of the same machine rather than two similar sets."""
    v = cfg["victim"]
    a = cfg["aggressors"]
    deploy = cfg["deploy"]["path"]
    metrics = [m for m in v["metrics"] if m in victims.MANIFEST]
    hw = []
    for m in metrics:
        for h in victims.get(m).hardware:
            hw.append((m, h))
    est = estimate(cfg)
    conds = list(a["conditions"])
    return {
        "session": cfg["session"],
        "victim_cpu": v["cpu"],
        "victim_priority": v["priority"],
        "aggressor_cpus": list(a["cpus"]),
        "aggressor_tool": a["tool"],
        "aggressor_conditions": conds,
        "aggressor_conditions_need_tool": any(c != "none" for c in conds),
        "stressor_cgroup": STRESSOR_CGROUP,
        "governor": cfg["preconditions"]["governor"],
        "rt_runtime_us": cfg["preconditions"]["rt_runtime_us"],
        "load_ceiling": cfg["preconditions"]["load_ceiling"],
        "free_mb_floor": cfg["preconditions"]["free_mb_floor"],
        "perf_event_paranoid_max":
            cfg["preconditions"]["perf_event_paranoid_max"],
        "counters_required": ("on" in cfg["counters"]["state"]),
        "output_dir": cfg["output"],
        "estimated_mb": int(est["megabytes"]),
        "victim_binaries": dict(
            (m, os.path.join(deploy, victims.get(m).binary)) for m in metrics),
        "hardware_preconditions": hw,
    }


def run_order(cfg):
    """Every run this session will perform, in the order it will perform it.

    Returned rather than executed so that `explain` can print exactly what
    `run` will do. Two descriptions of the ordering would eventually
    disagree, and the one printed is the one users would trust."""
    v = cfg["victim"]
    blocks = []
    for repeat in range(1, cfg["repeats"] + 1):
        for metric in v["metrics"]:
            for state in cfg["counters"]["state"]:
                runs = []
                for cond in cfg["aggressors"]["conditions"]:
                    tag = "%s-%s-i%d-r%d-%s" % (
                        metric, state, cfg["aggressors"]["intensity"],
                        repeat, CONDITION_TAG.get(cond, cond))
                    runs.append({"tag": tag, "metric": metric,
                                 "counters": state, "condition": cond,
                                 "repeat": repeat})
                blocks.append({"metric": metric, "counters": state,
                               "repeat": repeat, "warmup": True,
                               "runs": runs})
    return blocks


def victim_settings(cfg, metric, counter_state):
    """The settings for one victim invocation, declared options only."""
    v = cfg["victim"]
    victim = victims.get(metric)
    settings = {
        "cpu": v["cpu"],
        "iterations": v["iterations"],
        "priority": v["priority"],
        "execution_context": "host",
        "counter_set": cfg["counters"]["set"],
        "counters_off": (counter_state == "off"),
    }
    if v.get("warmup") is not None:
        settings["warmup"] = v["warmup"]
    for optional in ("priority_gap", "counter_backend", "boundary_timestamps",
                     "instrument_server", "counters_in_server", "atomic_mode",
                     "relaxed_scheduling"):
        value = v.get(optional)
        if value in (None, False):
            continue
        if not victim.supports(optional):
            # Caught in config validation; refused again here so that a
            # programmatic caller cannot bypass it.
            raise SessionError("%s does not accept %s" % (metric, optional))
        settings[optional] = value
    return settings


class Session(object):
    def __init__(self, cfg, adapter, log=None):
        self.cfg = cfg
        self.ad = adapter
        self.plan = build_plan(cfg, adapter)
        self.out = cfg["output"]
        self.log = log or (lambda *a: print(*a))
        self._aggressors = []

    # -- aggressors ----------------------------------------------------------

    def _cgroup_ready(self):
        root = "/sys/fs/cgroup"
        subtree = self.ad.read_text("%s/cgroup.subtree_control" % root, "") or ""
        if "cpuset" not in subtree.split():
            ok, err = self.ad.write_text("%s/cgroup.subtree_control" % root,
                                         "+cpuset")
            if not ok:
                raise SessionError(
                    "cannot delegate the cpuset controller: %s. The init "
                    "system withdraws this delegation, so it is re-checked "
                    "before every run rather than assumed." % err)
        os.makedirs(STRESSOR_CGROUP, exist_ok=True)
        want = ",".join(str(c) for c in self.plan["aggressor_cpus"])
        ok, err = self.ad.write_text("%s/cpuset.cpus" % STRESSOR_CGROUP, want)
        if not ok:
            raise SessionError("cannot set the aggressor cpuset: %s" % err)
        self.ad.write_text("%s/cpuset.mems" % STRESSOR_CGROUP, "0")
        effective = self.ad.read_text("%s/cpuset.cpus.effective"
                                      % STRESSOR_CGROUP, "")
        if preconditions._cpu_list(effective) != set(
                self.plan["aggressor_cpus"]):
            raise SessionError(
                "the aggressor cpuset is not effective: asked for %s, "
                "cpuset.cpus.effective reads %r. The effective set is the "
                "intersection with the parent's, so a request the parent "
                "does not hold yields a narrower set silently."
                % (want, effective))

    def start_aggressor(self, condition):
        if condition == "none":
            return {"condition": "none", "identity": None,
                    "parameters": None, "confinement_verified": True,
                    "processes": 0}
        self._cgroup_ready()
        io_tmp = self.cfg["aggressors"]["io_path"]
        commands = aggressor_command(
            condition, self.cfg["aggressors"]["intensity"],
            self.cfg["aggressors"]["io_bytes"], io_tmp)
        if condition == "io":
            os.makedirs(io_tmp, exist_ok=True)
        for argv in commands:
            h = self.ad.start(argv, stdout=os.devnull)
            self._aggressors.append(h)
        time.sleep(SETTLE_SECONDS)
        for h in self._aggressors:
            if h.poll() is not None:
                raise SessionError("the %s aggressor died immediately"
                                   % condition)
            ok, err = self.ad.write_text(
                "%s/cgroup.procs" % STRESSOR_CGROUP, str(h.pid))
            if not ok:
                raise SessionError("cannot confine aggressor pid %d: %s"
                                   % (h.pid, err))
        time.sleep(SETTLE_SECONDS)
        verified, procs = self._verify_confinement()
        return {
            "condition": condition,
            "identity": self.cfg["aggressors"]["tool"],
            "parameters": " | ".join(" ".join(c) for c in commands),
            "intensity": self.cfg["aggressors"]["intensity"],
            "confinement_verified": verified,
            "processes": procs,
            "cpus": self.plan["aggressor_cpus"],
            "io_path": io_tmp if condition in ("io",) else None,
            "io_filesystem": _filesystem_of(io_tmp)
                             if condition in ("io",) else None,
        }

    def _verify_confinement(self):
        """Every aggressor THREAD's allowed CPUs, not just the parent's.

        Confining a process after it has forked leaves its existing children
        where they were, and a child on the victim's core is not interference
        from another partition — it is the victim's core being shared."""
        want = ",".join(str(c) for c in self.plan["aggressor_cpus"])
        want_set = set(self.plan["aggressor_cpus"])
        rc, out, _ = self.ad.run(["pgrep", self.cfg["aggressors"]["tool"]],
                                 timeout=10)
        pids = [p for p in out.split() if p.isdigit()]
        for pid in pids:
            allowed = None
            status = self.ad.read_text("/proc/%s/status" % pid, "") or ""
            for line in status.splitlines():
                if line.startswith("Cpus_allowed_list:"):
                    allowed = line.split(None, 1)[1].strip()
            if allowed is None:
                continue
            if preconditions._cpu_list(allowed) != want_set:
                raise SessionError(
                    "aggressor pid %s is allowed CPUs %r, expected %s"
                    % (pid, allowed, want))
        return True, len(pids)

    def stop_aggressor(self):
        for h in self._aggressors:
            self.ad.stop(h)
        self._aggressors = []
        self.ad.run(["pkill", "-f", self.cfg["aggressors"]["tool"]],
                    timeout=20)
        time.sleep(TERMINATION_GRACE_SECONDS)
        rc, out, _ = self.ad.run(["pgrep", self.cfg["aggressors"]["tool"]],
                                 timeout=10)
        if out.strip():
            raise SessionError(
                "the aggressor is still running after termination was "
                "requested (pids %s). The next run is a baseline, and a load "
                "generator that outlives its own run contaminates it."
                % " ".join(out.split()))
        io_tmp = self.cfg["aggressors"]["io_path"]
        if os.path.isdir(io_tmp):
            for f in os.listdir(io_tmp):
                try:
                    os.remove(os.path.join(io_tmp, f))
                except OSError:
                    pass

    # -- one run -------------------------------------------------------------

    def warm_up(self):
        self.log("  warm-up %ds on all cores" % WARMUP_SECONDS)
        self.ad.run(["stress-ng", "--cpu", "0", "--cpu-method", "matrixprod",
                     "-t", "%ds" % WARMUP_SECONDS], timeout=WARMUP_SECONDS + 60)
        time.sleep(5)

    def run_one(self, spec):
        metric = spec["metric"]
        victim = victims.get(metric)
        settings = victim_settings(self.cfg, metric, spec["counters"])
        record = os.path.join(self.out, spec["tag"] + ".csv")

        # A record is written once. Every path this run will produce is
        # checked before anything starts, because a refusal after the run
        # loses the measurement rather than preventing it.
        produced = victim.records_for(record, settings)
        for _, path in produced:
            if os.path.exists(path):
                raise SessionError(
                    "%s already exists; records are written once. Remove the "
                    "previous session's output or choose another output "
                    "directory." % path)
        cpath = conditions_mod.conditions_path(record)
        if os.path.exists(cpath):
            raise SessionError("%s already exists" % cpath)

        free = self.ad.free_mb(self.out if os.path.isdir(self.out) else ".")
        if free < self.plan["free_mb_floor"]:
            raise SessionError(
                "%d MB free, floor is %d MB. A run that fills the filesystem "
                "loses the record it is writing."
                % (free, self.plan["free_mb_floor"]))

        agg = self.start_aggressor(spec["condition"])
        before = conditions_mod.capture(self.ad, self.plan, "before", agg)

        binary = self.plan["victim_binaries"][metric]
        argv = victim.command(binary, record, settings)
        self.log("  run %-34s %s" % (spec["tag"], " ".join(argv[1:])))
        t0 = time.time()
        h = self.ad.start(argv, stdout=os.path.join(self.out,
                                                    spec["tag"] + ".out"))
        rc = h.wait()
        elapsed = time.time() - t0

        after = conditions_mod.capture(self.ad, self.plan, "after", agg)
        if spec["condition"] != "none":
            self.stop_aggressor()

        pair = conditions_mod.pair(before, after, spec["tag"],
                                   self.cfg["session"])
        conditions_mod.write(pair, cpath)

        missing = [p for _, p in produced if not os.path.exists(p)]
        result = {
            "tag": spec["tag"], "metric": metric,
            "counters": spec["counters"], "condition": spec["condition"],
            "repeat": spec["repeat"], "returncode": rc,
            "seconds": elapsed, "records": [p for _, p in produced],
            "conditions": cpath, "drifted": pair["drifted"],
            "drift": pair["drift"], "missing_records": missing,
        }
        if rc != 0 or missing:
            result["failed"] = True
            self.log("    FAILED rc=%d%s" % (rc, ", missing %s" % missing
                                             if missing else ""))
        else:
            result["failed"] = False
            self.log("    ok  %.1fs%s" % (elapsed,
                                          "  DRIFT" if pair["drifted"] else ""))
        return result

    def run(self):
        os.makedirs(self.out, exist_ok=True)
        blocks = run_order(self.cfg)
        results = []
        for i, block in enumerate(blocks, 1):
            self.log("block %d/%d  %s counters=%s repeat %d"
                     % (i, len(blocks), block["metric"], block["counters"],
                        block["repeat"]))
            if block["warmup"]:
                self.warm_up()
            for spec in block["runs"]:
                try:
                    results.append(self.run_one(spec))
                except SessionError as e:
                    self.log("    FAILED %s: %s" % (spec["tag"], e))
                    results.append({"tag": spec["tag"], "failed": True,
                                    "error": str(e), "metric": spec["metric"],
                                    "condition": spec["condition"],
                                    "counters": spec["counters"],
                                    "repeat": spec["repeat"]})
                    try:
                        self.stop_aggressor()
                    except SessionError:
                        pass
        return results
