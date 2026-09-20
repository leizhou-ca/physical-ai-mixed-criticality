#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Preconditions: whether a measurement taken here would be valid.
#
# This is the command a new user runs first and the one they run after every
# failure, so its output is the product's first impression. Three rules shape
# it, and all three come from watching the shell scripts this replaces fail:
#
# 1. EVERY FAILURE IS REPORTED, never just the first. Fixing preconditions
#    one abort at a time means a user learns about the eighth problem forty
#    minutes after the first, and a user who has to do that eight times
#    stops. `check` runs everything it can and prints the lot.
#
# 2. EVERY FAILURE STATES THE OBSERVED VALUE, THE REQUIRED VALUE, WHY IT
#    MATTERS AND WHAT TO TYPE. A message reading "cpuset.cpus not writable"
#    names a symptom and cost this project twenty minutes of its own time to
#    trace back to a cause that had nothing to do with the file's
#    permissions. The remediation text is the deliverable here, not a
#    courtesy wrapped around the real work.
#
# 3. THERE ARE THREE STATES, NOT TWO. Pass and fail, and unmet-but-tolerable:
#    a domain with no tracing cannot do kernel-event attribution, which is a
#    limitation to report, not a reason to refuse to measure. Collapsing that
#    into "fail" would make the tool refuse to run on most machines;
#    collapsing it into "pass" would let a result claim a tier it never had.
#
# A precondition is here because this campaign hit it, not because it seemed
# prudent. Where the text below says what something did to a measurement, it
# is describing something that actually happened to one.
import os
import re

PASS = "pass"
FAIL = "fail"
TOLERABLE = "tolerable"
SKIPPED = "skipped"

STATE_MARK = {PASS: "ok", FAIL: "FAIL", TOLERABLE: "note", SKIPPED: "skip"}


class Result(object):
    __slots__ = ("number", "name", "state", "observed", "required", "why",
                 "fix", "then")

    def __init__(self, number, name, state, observed=None, required=None,
                 why=None, fix=None, then=None):
        self.number = number
        self.name = name
        self.state = state
        self.observed = observed
        self.required = required
        self.why = why
        self.fix = fix
        self.then = then

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def _cpu_number(path):
    """The N in .../cpuN/cpufreq/... — taken from the path component, because
    splitting on "/cpu" finds the parent directory first and yields nothing."""
    m = re.search(r"/cpu(\d+)/", path)
    return m.group(1) if m else "?"


def _cpu_list(spec):
    """Parse a kernel CPU list such as '2-3,5' into a set."""
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        elif part.isdigit():
            out.add(int(part))
    return out


# --------------------------------------------------------------- the twelve

def check_1_victim_cpu_isolated(ad, plan):
    isolated_raw = ad.read_text("/sys/devices/system/cpu/isolated", "") or ""
    isolated = _cpu_list(isolated_raw)
    cpu = plan["victim_cpu"]
    if cpu in isolated:
        return Result(1, "victim CPU isolated", PASS,
                      observed="isolated = %s, victim CPU %d is in it"
                               % (isolated_raw or "(empty)", cpu))
    return Result(
        1, "victim CPU isolated", FAIL,
        observed="isolated = %s; victim CPU %d is not in it"
                 % (isolated_raw or "(empty)", cpu),
        required="the victim's core must carry no other runnable work",
        why="a victim sharing its core with the scheduler's ordinary work "
            "measures that work too. The interference then appears in the "
            "tail exactly where an aggressor's would, and no amount of "
            "analysis afterwards can separate the two.",
        fix="add isolcpus=%d to the kernel command line and reboot" % cpu,
        then="read /sys/devices/system/cpu/isolated back afterwards. The "
             "command line is a request; this file is the effect, and they "
             "are not always the same thing — see precondition 11.")


def check_2_clock_pinned(ad, plan):
    governors, currents, maxima, bad = {}, {}, {}, []
    want = plan["governor"]
    for path in ad.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        cpu = _cpu_number(path)
        g = ad.read_text(path)
        cur = ad.read_text(path.replace("scaling_governor", "scaling_cur_freq"))
        mx = ad.read_text(path.replace("scaling_governor", "scaling_max_freq"))
        governors[cpu], currents[cpu], maxima[cpu] = g, cur, mx
        if g != want:
            bad.append(("governor=%s" % g, cpu))
        elif want == "performance" and cur and mx and cur != mx:
            bad.append(("at %s kHz, max %s kHz" % (cur, mx), cpu))
    if not governors:
        return Result(2, "clock pinned", TOLERABLE,
                      observed="no cpufreq interface in this domain",
                      why="the clock cannot be pinned or read here, so a "
                          "comparison between runs cannot be checked for "
                          "clock drift. Results remain valid within a run.")
    if not bad:
        return Result(2, "clock pinned", PASS,
                      observed="all %d CPUs governor=%s, each at its maximum"
                               % (len(governors), want))
    # Identical findings are grouped. Sixteen lines saying the same thing
    # about sixteen cores buries the one core that differs.
    grouped = {}
    for text, cpu in bad:
        grouped.setdefault(text, []).append(cpu)
    def _cores(cpus):
        n = sorted(cpus, key=lambda c: int(c) if c.isdigit() else -1)
        if len(n) < 5:
            return ",".join(n)
        return "%s..%s (%d cores)" % (n[0], n[-1], len(n))
    observed = "; ".join("cpu%s %s" % (_cores(cpus), text)
                         for text, cpus in sorted(grouped.items()))
    return Result(
        2, "clock pinned", FAIL,
        observed=observed,
        required="every CPU governor = %s, and the current frequency equal "
                 "to the maximum" % want,
        why="this is the failure that cost this campaign a set of published "
            "medians. Per-stressor figures were taken with the governor left "
            "at its default; re-measurement with the clock pinned showed the "
            "differences between stressors had been dominated by clock "
            "frequency rather than by the stressors. The numbers were not "
            "wrong by a little — the ranking they supported was an artefact "
            "of the governor.",
        fix="for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; "
            "do echo %s > $c; done" % want,
        then="re-run check. Writing the governor does not guarantee the "
             "frequency followed it — thermal or firmware limits can hold a "
             "core below its maximum with the governor set correctly, which "
             "is why this reads the current frequency back rather than "
             "trusting the governor alone.")


def check_3_rt_throttling(ad, plan):
    path = "/proc/sys/kernel/sched_rt_runtime_us"
    observed = ad.read_text(path)
    want = str(plan["rt_runtime_us"])
    if observed is None:
        return Result(3, "RT throttling as declared", TOLERABLE,
                      observed="%s is not present" % path,
                      why="this kernel does not expose real-time throttling, "
                          "so it cannot be disabled or confirmed.")
    if observed == want:
        return Result(3, "RT throttling as declared", PASS,
                      observed="sched_rt_runtime_us = %s%s"
                               % (observed,
                                  " (throttling disabled)" if want == "-1"
                                  else ""))
    return Result(
        3, "RT throttling as declared", FAIL,
        observed="sched_rt_runtime_us = %s" % observed,
        required="sched_rt_runtime_us = %s" % want,
        why="with throttling active the kernel stops real-time tasks for the "
            "remainder of each period once they have used their share. The "
            "victim runs at SCHED_FIFO, so it is throttled — and the "
            "resulting stall lands in the latency tail, where it is "
            "indistinguishable from the interference under study. The "
            "measurement does not fail; it quietly measures the throttler.",
        fix="echo %s > %s" % (want, path),
        then="this resets on reboot, so it belongs in the run procedure "
             "rather than in a machine's setup.")


def check_4_counter_access(ad, plan):
    path = "/proc/sys/kernel/perf_event_paranoid"
    raw = ad.read_text(path)
    ceiling = plan["perf_event_paranoid_max"]
    observed = "perf_event_paranoid = %s" % (raw if raw is not None
                                             else "(absent)")
    opened, why_not = _try_counter_open(ad)
    if opened:
        return Result(4, "counter access", PASS,
                      observed="%s; a counter opened successfully" % observed)
    if plan["counters_required"]:
        state = FAIL
    else:
        state = TOLERABLE
    return Result(
        4, "counter access", state,
        observed="%s; opening a counter failed: %s" % (observed, why_not),
        required="perf_event_open must succeed for the measuring context, "
                 "which usually means perf_event_paranoid at or below %d, or "
                 "privilege" % ceiling,
        why="the paranoid setting alone does not answer this. It can permit "
            "the open and the open still fail — no PMU exposed to a guest, a "
            "container without the capability, a counter set the part does "
            "not implement. So this opens one rather than reading a number "
            "and hoping. Without counters the run still measures latency; it "
            "produces no hardware-channel attribution."
            + ("" if plan["counters_required"] else
               " This session did not ask for counters, so this is a "
               "limitation rather than a failure."),
        fix="echo %d > %s   (or run as root)" % (ceiling, path),
        then="re-run check; it will report whether the open now succeeds, "
             "which is the question, rather than whether the setting "
             "changed.")


def _try_counter_open(ad):
    """Actually open a counter. Returns (ok, reason)."""
    try:
        import ctypes
        import ctypes.util
    except ImportError:                                  # pragma: no cover
        return False, "ctypes unavailable"
    syscalls = {"aarch64": 241, "x86_64": 298, "armv7l": 364}
    machine = os.uname().machine
    if machine not in syscalls:
        return False, "perf_event_open syscall number unknown for %s" % machine

    class Attr(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                    ("config", ctypes.c_uint64),
                    ("sample_period", ctypes.c_uint64),
                    ("sample_type", ctypes.c_uint64),
                    ("read_format", ctypes.c_uint64),
                    ("flags", ctypes.c_uint64),
                    ("wakeup_events", ctypes.c_uint32),
                    ("bp_type", ctypes.c_uint32),
                    ("config1", ctypes.c_uint64), ("config2", ctypes.c_uint64),
                    ("branch_sample_type", ctypes.c_uint64),
                    ("sample_regs_user", ctypes.c_uint64),
                    ("sample_stack_user", ctypes.c_uint32),
                    ("clockid", ctypes.c_int32),
                    ("sample_regs_intr", ctypes.c_uint64),
                    ("aux_watermark", ctypes.c_uint32),
                    ("sample_max_stack", ctypes.c_uint16),
                    ("res2", ctypes.c_uint16),
                    ("aux_sample_size", ctypes.c_uint32),
                    ("res3", ctypes.c_uint32),
                    ("sig_data", ctypes.c_uint64),
                    ("config3", ctypes.c_uint64)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    a = Attr()
    a.size = ctypes.sizeof(a)
    a.type = 0                      # PERF_TYPE_HARDWARE
    a.config = 0                    # PERF_COUNT_HW_CPU_CYCLES
    a.flags = (1 << 0) | (1 << 5) | (1 << 6)   # disabled, exclude_kernel, hv
    fd = libc.syscall(syscalls[machine], ctypes.byref(a), ctypes.c_int(0),
                      ctypes.c_int(-1), ctypes.c_int(-1), ctypes.c_ulong(0))
    if fd < 0:
        return False, os.strerror(ctypes.get_errno())
    os.close(fd)
    return True, None


def check_5_cgroup_delegated(ad, plan):
    root = "/sys/fs/cgroup"
    controllers = ad.read_text("%s/cgroup.controllers" % root, "") or ""
    subtree = ad.read_text("%s/cgroup.subtree_control" % root, "") or ""
    if "cpuset" not in controllers.split():
        return Result(
            5, "cgroup cpuset delegated", FAIL,
            observed="cpuset is not among the root controllers (%s)"
                     % (controllers or "none"),
            required="the cpuset controller available at the cgroup root",
            why="without the cpuset controller the aggressors cannot be "
                "confined to their own cores, and an aggressor free to run "
                "on the victim's core is not an aggressor in another "
                "partition — it is the victim's own core being shared, which "
                "measures something else entirely.",
            fix="boot with cgroup v2 and the cpuset controller available "
                "(cgroup_enable=cpuset on some distributions)",
            then="re-run check.")
    if "cpuset" in subtree.split():
        return Result(5, "cgroup cpuset delegated", PASS,
                      observed="cpuset is delegated to the subtree")
    return Result(
        5, "cgroup cpuset delegated", FAIL,
        observed="cpuset is available at the root but not delegated to the "
                 "subtree (subtree_control = %s)" % (subtree or "empty"),
        required="cpuset present in cgroup.subtree_control",
        why="THIS IS CHECKED BEFORE EVERY RUN, not once at setup, because "
            "the init system withdraws the delegation. It is put back by "
            "hand, a session runs, systemd reloads or a unit changes, and the "
            "next session finds it gone. The symptom is a write to "
            "cpuset.cpus failing with a permission error, which sends a "
            "reader to look at file ownership — where there is nothing "
            "wrong. That misdirection cost this project twenty minutes, and "
            "it is the reason this precondition names the cause instead of "
            "the symptom.",
        fix="echo +cpuset > %s/cgroup.subtree_control" % root,
        then="expect to do this again. It is a per-session precondition, not "
             "a one-time setup step, and the run procedure re-checks it "
             "rather than assuming a previous session's fix survived.")


def check_6_stressor_cpuset_effective(ad, plan):
    group = plan["stressor_cgroup"]
    want = plan["aggressor_cpus"]
    eff_path = "%s/cpuset.cpus.effective" % group
    if not ad.exists(group):
        return Result(6, "stressor cpuset effective", SKIPPED,
                      observed="%s does not exist yet" % group,
                      why="the session creates this group when it starts; "
                          "the effective set can only be read once it "
                          "exists, and is verified then.")
    effective = _cpu_list(ad.read_text(eff_path, "") or "")
    requested = _cpu_list(ad.read_text("%s/cpuset.cpus" % group, "") or "")
    if effective == set(want):
        return Result(6, "stressor cpuset effective", PASS,
                      observed="effective = %s"
                               % (ad.read_text(eff_path) or ""))
    return Result(
        6, "stressor cpuset effective", FAIL,
        observed="requested %s, effective %s"
                 % (sorted(requested) or "nothing", sorted(effective) or "nothing"),
        required="effective set exactly %s" % sorted(want),
        why="a cpuset write can succeed and have no effect. The effective "
            "set is the intersection of what was asked for with what the "
            "parent group allows, so asking for cores the parent does not "
            "hold yields a narrower set, silently — and in the worst case an "
            "empty one, where the aggressors run wherever the scheduler "
            "likes, including on the victim's core. This reads "
            "cpuset.cpus.effective and never cpuset.cpus, because only the "
            "first of those is what happened.",
        fix="ensure the parent cgroup holds %s, then write them to "
            "%s/cpuset.cpus" % (sorted(want), group),
        then="re-read cpuset.cpus.effective. The session also verifies every "
             "aggressor THREAD's affinity after starting, because confining "
             "the parent process does not confine children it has already "
             "forked.")


def check_7_board_quiet(ad, plan):
    raw = ad.read_text("/proc/loadavg", "") or ""
    try:
        load1 = float(raw.split()[0])
    except (IndexError, ValueError):
        return Result(7, "board quiet", TOLERABLE,
                      observed="could not read /proc/loadavg")
    ceiling = plan["load_ceiling"]
    busy = _busy_processes(ad)
    if load1 <= ceiling:
        return Result(7, "board quiet", PASS,
                      observed="1-minute load %.2f, ceiling %.2f"
                               % (load1, ceiling))
    return Result(
        7, "board quiet", FAIL,
        observed="1-minute load %.2f, ceiling %.2f%s"
                 % (load1, ceiling,
                    "; running or uninterruptible: %s" % busy if busy else ""),
        required="1-minute load at or below %.2f before the first run"
                 % ceiling,
        why="work already running competes for the memory system and the "
            "shared cache even when it is confined to other cores, and a "
            "baseline taken on a busy board is not a baseline. The point of "
            "a differential is that the only difference between the two "
            "sides is the aggressor.",
        fix="stop whatever is running, or wait for it to finish — "
            "%s" % (busy or "check with: ps -eo stat,comm | grep -E '^[RD]'"),
        then="a previous session's aggressors are the usual cause. The "
             "session kills and confirms termination between runs, but an "
             "interrupted session may have left some behind: pkill stress-ng")


def _busy_processes(ad):
    rc, out, _ = (0, "", "")
    try:
        rc, out, _ = ad.run(["ps", "-eo", "stat,comm"], timeout=10)
    except Exception:
        return ""
    names = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0][:1] in ("R", "D"):
            if parts[1] not in ("ps",):
                names[parts[1]] = names.get(parts[1], 0) + 1
    return ", ".join("%s x%d" % (k, v) for k, v in sorted(names.items()))


def check_8_free_space(ad, plan):
    path = plan["output_dir"]
    # Walk up to the nearest existing ancestor: the output directory may not
    # have been created yet, and the free space that matters is the
    # filesystem it will land on. The loop stops when the path stops
    # changing, because the dirname of "/" is "/" and a condition that only
    # ever tested existence would spin there forever.
    probe = path if ad.exists(path) else os.path.dirname(os.path.abspath(path))
    while probe and not ad.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    free = ad.free_mb(probe if ad.exists(probe) else "/")
    floor = plan["free_mb_floor"]
    need = plan["estimated_mb"]
    if free >= max(floor, need):
        return Result(8, "free space", PASS,
                      observed="%d MB free at %s; this session is estimated "
                               "to need %d MB" % (free, probe, need))
    return Result(
        8, "free space", FAIL,
        observed="%d MB free at %s" % (free, probe),
        required="at least %d MB — the floor of %d MB, and %d MB estimated "
                 "for this session" % (max(floor, need), floor, need),
        why="a record is written once and refuses to overwrite, so a run "
            "that fills the filesystem does not fail cleanly and retry: it "
            "loses the record it was writing, and the session cannot simply "
            "be restarted at that arm because the arms before it already "
            "hold their records. This campaign ran a board to 80% full and "
            "had to collect and delete after every repeat to finish a "
            "three-repeat sequence.",
        fix="free space, or reduce iterations or repeats — mcib explain "
            "<config> prints the estimate this check is using",
        then="the session re-checks this between runs as well as before the "
             "first, because the records themselves are what fills the "
             "disk.")


def check_9_victim_binaries(ad, plan):
    missing, not_exec, ok = [], [], []
    for metric, path in sorted(plan["victim_binaries"].items()):
        if not ad.exists(path):
            missing.append("%s (%s)" % (metric, path))
        elif not os.access(path, os.X_OK):
            not_exec.append("%s (%s)" % (metric, path))
        else:
            ok.append(metric)
    if not missing and not not_exec:
        return Result(9, "victim binary present and runnable", PASS,
                      observed="%s present and executable" % ", ".join(ok))
    problems = []
    if missing:
        problems.append("missing: %s" % ", ".join(missing))
    if not_exec:
        problems.append("not executable: %s" % ", ".join(not_exec))
    return Result(
        9, "victim binary present and runnable", FAIL,
        observed="; ".join(problems),
        required="every metric named in the config has its binary, built for "
                 "this machine, at the deploy path",
        why="the victim is the measuring instrument. It is built from source "
            "for the target because it links the probe library, and a "
            "session that reaches the board without it wastes the setup and "
            "the warm-up before failing.",
        fix="build the victims for this target and deploy them, or correct "
            "`deploy.path` in the config",
        then="check that they were built for this architecture (%s). A "
             "binary for the wrong architecture is present and executable "
             "and still will not run." % os.uname().machine)


def check_10_aggressor_tool(ad, plan):
    if not plan["aggressor_conditions_need_tool"]:
        return Result(10, "aggressor tool present", SKIPPED,
                      observed="this session declares no aggressor beyond "
                               "the baseline")
    tool = plan["aggressor_tool"]
    found = ad.which(tool)
    if found:
        rc, out, err = ad.run([tool, "--version"], timeout=20)
        version = (out or err).strip().splitlines()[0] if (out or err) else ""
        return Result(10, "aggressor tool present", PASS,
                      observed="%s at %s%s"
                               % (tool, found,
                                  " — %s" % version if version else ""))
    return Result(
        10, "aggressor tool present", FAIL,
        observed="%s is not on PATH" % tool,
        required="%s, for the conditions this session declares: %s"
                 % (tool, ", ".join(plan["aggressor_conditions"])),
        why="the framework does not generate load. It measures whether an "
            "isolation mechanism bounds a tail, and the load comes from a "
            "tool the wider community maintains rather than from a "
            "reimplementation whose behaviour nobody else could reproduce.",
        fix="install %s from your distribution, or declare only the baseline "
            "condition" % tool,
        then="the aggressor's parameters are recorded with the run. A capped "
             "and an uncapped I/O aggressor reversed an aggressor ranking in "
             "this campaign's own data, so the parameters matter as much as "
             "the tool.")


def check_11_isolation_parameters_active(ad, plan):
    """Reports state; never fails. A command-line entry is a request."""
    cmdline = ad.read_text("/proc/cmdline", "") or ""
    requested = {}
    for token in cmdline.split():
        for key in ("isolcpus", "nohz_full", "rcu_nocbs"):
            if token.startswith(key + "="):
                requested[key] = token.split("=", 1)[1]
    active = {
        "isolcpus": ad.read_text("/sys/devices/system/cpu/isolated", "") or "",
        "nohz_full": ad.read_text("/sys/devices/system/cpu/nohz_full", None),
        "rcu_nocbs": None,
    }
    inert, confirmed = [], []
    for key, want in sorted(requested.items()):
        got = active.get(key)
        if got is None:
            inert.append("%s=%s requested, no sysfs file reports it as "
                         "active" % (key, want))
        elif _cpu_list(got) >= _cpu_list(want) and _cpu_list(want):
            confirmed.append("%s=%s active" % (key, want))
        else:
            inert.append("%s=%s requested, active set is %r"
                         % (key, want, got or ""))
    if not requested:
        return Result(11, "isolation parameters ACTIVE", TOLERABLE,
                      observed="no isolation parameters on the kernel "
                               "command line",
                      why="nothing was requested, so nothing can be inert. "
                          "Precondition 1 is what decides whether the "
                          "victim's core is actually isolated.")
    if not inert:
        return Result(11, "isolation parameters ACTIVE", PASS,
                      observed="; ".join(confirmed))
    return Result(
        11, "isolation parameters ACTIVE", TOLERABLE,
        observed="; ".join(inert + confirmed),
        required="nothing — this is reported, never failed",
        why="A KERNEL COMMAND LINE ENTRY IS A REQUEST, NOT AN EFFECT. This "
            "campaign's board asks for nohz_full and rcu_nocbs on its "
            "command line and the kernel rejects both, because it was built "
            "without the options that implement them. It logs them as "
            "unknown parameters and passes them through to user space, where "
            "they sit in /proc/cmdline looking exactly like the ones that "
            "worked. Reading the command line would have confirmed an "
            "isolation that was never in effect. The measurement is still "
            "valid; it is simply not measuring the configuration its "
            "operator believes they set, so the state is recorded with the "
            "run and reported here.",
        fix="nothing is required. If the isolation was intended, the kernel "
            "must be built with the options that implement it — check dmesg "
            "for 'Unknown kernel command line parameters'",
        then="the conditions block records the ACTIVE state, as read back, "
             "not the requested one, so a result can never be read as "
             "claiming an isolation it did not have.")


def check_12_hardware(ad, plan):
    needs = plan["hardware_preconditions"]
    if not needs:
        return Result(12, "hardware preconditions", SKIPPED,
                      observed="no metric in this session declares one")
    lines = []
    for metric, hw in needs:
        lines.append("%s needs %s — %s" % (metric, hw.name, hw.detail))
    return Result(
        12, "hardware preconditions", TOLERABLE,
        observed="; ".join("%s needs %s" % (m, h.name) for m, h in needs),
        required="declared by the manifest, verified by the victim itself",
        why="a wiring precondition cannot be read from a file. The victim "
            "checks it directly — the interrupt metric drives its output "
            "line and confirms continuity before it measures anything — and "
            "refuses to run rather than record intervals that measure a "
            "disconnected pin. This reports what the session will need so "
            "that a user finds out now rather than after the warm-up.\n"
            + "\n".join("    %s" % line for line in lines),
        fix="; ".join(h.remedy for _, h in needs),
        then="run the victim once by hand with a small iteration count to "
             "confirm the wiring before committing to a full session.")


ALL_CHECKS = (check_1_victim_cpu_isolated, check_2_clock_pinned,
              check_3_rt_throttling, check_4_counter_access,
              check_5_cgroup_delegated, check_6_stressor_cpuset_effective,
              check_7_board_quiet, check_8_free_space,
              check_9_victim_binaries, check_10_aggressor_tool,
              check_11_isolation_parameters_active, check_12_hardware)


def check_all(adapter, plan):
    """Run every precondition. One raising does not stop the others: a check
    that cannot answer reports that it could not, and the rest still run."""
    results = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn(adapter, plan))
        except Exception as e:                           # pragma: no cover
            n = int(fn.__name__.split("_")[1])
            results.append(Result(
                n, fn.__name__, FAIL,
                observed="the check itself failed: %s: %s"
                         % (type(e).__name__, e),
                why="this is a defect in the checker, not necessarily a "
                    "problem with the machine.",
                fix="report it with the message above"))
    return results


def summary(results):
    counts = {PASS: 0, FAIL: 0, TOLERABLE: 0, SKIPPED: 0}
    for r in results:
        counts[r.state] += 1
    return counts


def render(results, capabilities=None, stream=None, verbose=False):
    """The text a user reads. Failures carry their remediation in full;
    passes stay on one line, so that a screen of output is mostly the things
    that need attention."""
    import sys
    stream = stream or sys.stdout
    p = lambda *a: print(*a, file=stream)
    for r in sorted(results, key=lambda x: x.number):
        mark = {PASS: "  ok  ", FAIL: "  ✗   ", TOLERABLE: "  !   ",
                SKIPPED: "  -   "}[r.state]
        p("%s%2d  %s" % (mark, r.number, r.name))
        if r.state == PASS and not verbose:
            if r.observed:
                p("          %s" % r.observed)
            continue
        if r.observed:
            p("          %s" % r.observed)
        if r.required:
            p("          required: %s" % r.required)
        if r.why:
            p("")
            for line in _wrap(r.why, 68):
                p("          %s" % line)
        if r.fix:
            p("")
            p("          Fix:  %s" % r.fix)
        if r.then:
            for line in _wrap("Then: " + r.then, 68):
                p("          %s" % line)
        p("")
    if capabilities:
        p("capabilities")
        for line in capabilities:
            p("      %s" % line)
        p("")
    c = summary(results)
    p("%d passed, %d failed, %d reported as limitations, %d not applicable"
      % (c[PASS], c[FAIL], c[TOLERABLE], c[SKIPPED]))
    if c[FAIL]:
        p("")
        p("This session will not produce a valid measurement until the "
          "failures above are fixed.")
    return c[FAIL] == 0


def _wrap(text, width):
    out = []
    for para in text.split("\n"):
        if para.startswith("    "):
            out.append(para)
            continue
        words, line = para.split(), ""
        for w in words:
            if line and len(line) + 1 + len(w) > width:
                out.append(line)
                line = w
            else:
                line = (line + " " + w) if line else w
        if line:
            out.append(line)
    return out
