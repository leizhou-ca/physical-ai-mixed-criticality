#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Kernel event collection for the trace tier. Runs ON the target; everything
# else in this directory runs off it.
#
# WHY PERF-COLLECTED TRACEPOINTS RATHER THAN THE TRACING FILESYSTEM. Both
# routes were opened on one tracepoint and measured on the reference board,
# because the choice is decidable and an argument about it would not be.
#
#   Cost. Both routes cost the same, to within what the experiment can
#   resolve. Collecting the scheduler-switch tracepoint on a voluntary
#   task-switching workload added 1.2-1.9 us at the median and the 99th
#   percentile against an 8.6 us signal, by either route, and the difference
#   BETWEEN the routes was under a microsecond and changed sign from one
#   quantile to the next. Cost does not choose between them. That the cost is
#   14-22% of the signal is itself worth knowing, and is why a capture
#   records what it cost.
#
#   Timestamp resolution. This does choose between them. The tracing
#   filesystem's text interface emits seconds to six decimal places: across
#   202,025 events every timestamp was an exact multiple of 1000 ns. Perf
#   samples carry the full nanosecond. Measured against the probe's own
#   kernel reading on the same events, the offset from the opening mark to
#   the switch spans 333 ns between the 1st and 99th percentile when
#   collected by perf, and 1244 ns when collected as text — the same
#   distribution, smeared by the quantisation. A tier that places an event
#   inside a window whose typical width is 8.6 us should not throw away three
#   digits to do it.
#
#   State. Perf events are per-CPU file descriptors owned by this process and
#   released when it exits. The tracing filesystem is global: its clock,
#   event set, CPU mask and buffer size are machine-wide settings that a
#   collector must take over and put back, and that any other user of the
#   machine can change underneath it. There is nothing to restore here
#   because nothing global was taken.
#
# Both routes captured all 202,025 events with zero drops, and neither
# produced a single out-of-order event against the probe's marks, so neither
# was excluded on correctness.
#
# THE CAPTURE IS THE CONTRACT. Analysis reads the file this writes and never
# this module, so a different collection route is a change here and nowhere
# else. A capture states its own source, clock, event set, buffer size,
# window and drop count, because a capture that does not say what it is
# cannot be checked — and a capture reporting a dropped event is refused by
# the analysis rather than enriched over the hole it leaves.
import ctypes
import ctypes.util
import errno
import mmap
import os
import struct
import sys
import time

CAPTURE_KIND = "mcib.trace_capture"
CAPTURE_VERSION = 1

# perf_event_open constants, from the kernel's ABI header. These are the
# ABI's numbers, not tunable values of this project.
PERF_TYPE_TRACEPOINT = 2
PERF_SAMPLE_TID = 1 << 1
PERF_SAMPLE_TIME = 1 << 2
PERF_SAMPLE_CPU = 1 << 7
PERF_RECORD_LOST = 2
PERF_RECORD_SAMPLE = 9
PERF_EVENT_IOC_ENABLE = 0x2400
PERF_EVENT_IOC_DISABLE = 0x2401
ATTR_DISABLED = 1 << 0
ATTR_EXCLUDE_HV = 1 << 6
ATTR_EXCLUDE_IDLE = 1 << 7
ATTR_USE_CLOCKID = 1 << 25
CLOCK_MONOTONIC = 1
CLOCK_MONOTONIC_NAME = "CLOCK_MONOTONIC"

# The syscall number is per-architecture. Only the architectures this has
# been checked on are listed; an unlisted one fails loudly rather than
# issuing an arbitrary syscall.
PERF_EVENT_OPEN_SYSCALL = {"aarch64": 241, "x86_64": 298, "armv7l": 364}

TRACEFS_CANDIDATES = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")

# Collection parameters, not analysis thresholds. A threshold decides what a
# result means and belongs in the threshold inventory; these decide only how
# much kernel memory the ring takes and which events are asked for, are
# recorded verbatim in every capture, and are overridable per run. A wrong
# value here cannot bias a finding — it can only drop events, and a capture
# that drops events is refused rather than analysed.
DEFAULT_EVENTS = (
    "irq:irq_handler_entry",
    "irq:irq_handler_exit",
    "irq:softirq_entry",
    "irq:softirq_exit",
    "sched:sched_switch",
    "sched:sched_waking",
)
DEFAULT_RING_PAGES = 4096      # per event, per CPU, plus one control page


class perf_event_attr(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64), ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64), ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32), ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64), ("config2", ctypes.c_uint64),
        ("branch_sample_type", ctypes.c_uint64),
        ("sample_regs_user", ctypes.c_uint64),
        ("sample_stack_user", ctypes.c_uint32), ("clockid", ctypes.c_int32),
        ("sample_regs_intr", ctypes.c_uint64),
        ("aux_watermark", ctypes.c_uint32),
        ("sample_max_stack", ctypes.c_uint16), ("reserved_2", ctypes.c_uint16),
        ("aux_sample_size", ctypes.c_uint32), ("reserved_3", ctypes.c_uint32),
        ("sig_data", ctypes.c_uint64), ("config3", ctypes.c_uint64),
    ]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def tracefs():
    for p in TRACEFS_CANDIDATES:
        if os.path.isdir(os.path.join(p, "events")):
            return p
    raise SystemExit("no tracefs mount found; tracepoint ids cannot be read")


def tracepoint_id(name):
    """The kernel's numeric id for `subsystem:event`.

    Read from the tracing filesystem, which is where the kernel publishes it.
    Reading a file there is not the same as taking over the tracing
    filesystem's global state: nothing is written and nothing is left
    changed."""
    try:
        subsystem, event = name.split(":", 1)
    except ValueError:
        raise SystemExit("event %r is not in subsystem:event form" % name)
    path = os.path.join(tracefs(), "events", subsystem, event, "id")
    if not os.path.exists(path):
        raise SystemExit("tracepoint %s is not present on this kernel" % name)
    with open(path) as f:
        return int(f.read().strip())


def _syscall_number():
    machine = os.uname().machine
    if machine not in PERF_EVENT_OPEN_SYSCALL:
        raise SystemExit(
            "perf_event_open syscall number is not known for %s; add it "
            "rather than guessing" % machine)
    return PERF_EVENT_OPEN_SYSCALL[machine]


class _Ring(object):
    """One tracepoint on one CPU, and the ring it samples into."""

    def __init__(self, name, tp_id, cpu, pages):
        attr = perf_event_attr()
        attr.size = ctypes.sizeof(attr)
        attr.type = PERF_TYPE_TRACEPOINT
        attr.config = tp_id
        attr.sample_period = 1
        attr.sample_type = PERF_SAMPLE_TID | PERF_SAMPLE_TIME | PERF_SAMPLE_CPU
        # The clock is selected HERE, at open, and named in the capture. This
        # is the whole basis of the join: the events and the probe's own
        # kernel reading are then the same clock, so an event either falls
        # inside a measured interval or it does not, with no anchor to
        # estimate and no offset to correct.
        attr.flags = (ATTR_DISABLED | ATTR_EXCLUDE_HV | ATTR_EXCLUDE_IDLE
                      | ATTR_USE_CLOCKID)
        attr.clockid = CLOCK_MONOTONIC
        fd = _libc.syscall(_syscall_number(), ctypes.byref(attr),
                           ctypes.c_int(-1), ctypes.c_int(cpu),
                           ctypes.c_int(-1), ctypes.c_ulong(0))
        if fd < 0:
            err = ctypes.get_errno()
            hint = ""
            if err == errno.EACCES or err == errno.EPERM:
                hint = ("; tracepoint sampling needs privilege or a "
                        "perf_event_paranoid setting that permits it")
            raise SystemExit("perf_event_open(%s, cpu %d) failed: %s%s"
                             % (name, cpu, os.strerror(err), hint))
        self.name = name
        self.tp_id = tp_id
        self.cpu = cpu
        self.pages = pages
        self.fd = fd
        self.map_bytes = (pages + 1) * mmap.PAGESIZE
        self.buf = mmap.mmap(fd, self.map_bytes, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        self.samples = 0
        self.lost = 0

    # offsets into struct perf_event_mmap_page
    _HEAD, _TAIL = 1024, 1032

    def enable(self):
        _libc.ioctl(self.fd, PERF_EVENT_IOC_ENABLE, 0)

    def disable(self):
        _libc.ioctl(self.fd, PERF_EVENT_IOC_DISABLE, 0)

    def drain(self, sink):
        """Read every complete record the kernel has published.

        Draining happens once, after collection has stopped, rather than
        continuously while the workload runs: polling the ring during the
        measured window charges the workload for the collector's own
        scheduling, which is exactly the cost the capture is supposed to be
        reporting rather than causing."""
        data_off = mmap.PAGESIZE
        data_sz = self.pages * mmap.PAGESIZE
        head = struct.unpack_from("<Q", self.buf, self._HEAD)[0]
        tail = struct.unpack_from("<Q", self.buf, self._TAIL)[0]
        while tail < head:
            pos = data_off + (tail % data_sz)
            etype, _misc, esize = struct.unpack_from("<IHH", self.buf, pos)
            if esize == 0:
                break
            if (tail % data_sz) + esize > data_sz:
                first = data_sz - (tail % data_sz)
                rec = (self.buf[pos:data_off + data_sz]
                       + self.buf[data_off:data_off + esize - first])
            else:
                rec = self.buf[pos:pos + esize]
            if etype == PERF_RECORD_SAMPLE:
                pid, tid, t, cpu, _res = struct.unpack_from("<IIQII", rec, 8)
                sink.append((t, cpu, self.name, pid, tid))
                self.samples += 1
            elif etype == PERF_RECORD_LOST:
                _id, lost = struct.unpack_from("<QQ", rec, 8)
                self.lost += lost
            tail += esize
        struct.pack_into("<Q", self.buf, self._TAIL, tail)

    def close(self):
        self.buf.close()
        os.close(self.fd)


class Collector(object):
    """Open the event set, run it over a window, write one capture."""

    def __init__(self, cpu, events=DEFAULT_EVENTS, pages=DEFAULT_RING_PAGES):
        self.cpu = cpu
        self.events = tuple(events)
        self.pages = pages
        self.rings = []
        self.begin_ns = None
        self.end_ns = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def start(self):
        for name in self.events:
            self.rings.append(_Ring(name, tracepoint_id(name), self.cpu,
                                    self.pages))
        self.begin_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        for r in self.rings:
            r.enable()

    def stop(self):
        if self.end_ns is not None:
            return
        for r in self.rings:
            r.disable()
        self.end_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)

    def capture(self, measured_window=None):
        """Drain the rings and return the capture as a plain structure."""
        self.stop()
        rows = []
        for r in self.rings:
            r.drain(rows)
        rows.sort()
        dropped = sum(r.lost for r in self.rings)
        return {
            "kind": CAPTURE_KIND,
            "version": CAPTURE_VERSION,
            "source": "perf-tracepoint",
            "clock": CLOCK_MONOTONIC_NAME,
            "clock_selected_at_open": True,
            "cpu": self.cpu,
            "events": list(self.events),
            "ring_pages_per_event": self.pages,
            "ring_bytes_per_event": self.pages * mmap.PAGESIZE,
            "collection_begin_kernel_ns": self.begin_ns,
            "collection_end_kernel_ns": self.end_ns,
            "measured_window": measured_window,
            "dropped": dropped,
            "samples": len(rows),
            "samples_by_event": dict((r.name, r.samples) for r in self.rings),
            "rows": rows,
        }

    def close(self):
        for r in self.rings:
            r.close()
        self.rings = []


def measured_window_from_record(path):
    """The victim's own statement of the window it measured, in both clock
    domains.

    The counter domain is the victim's, not the collector's: the victim is
    the instrument that owns that clock and already publishes its run bounds
    in both. Re-deriving them here would be a second path to the same
    numbers, and the collector has no access to the victim's time source in
    any case."""
    wanted = ("run.begin_kernel_ns", "run.end_kernel_ns", "run.begin_wall",
              "run.end_wall", "domain.frequency_hz", "domain.id",
              "domain.counter_identity", "metric", "context")
    hdr = {}
    with open(path) as f:
        for line in f:
            if not line.startswith("# "):
                if line.startswith("context_id"):
                    break
                continue
            k, _, v = line[2:].rstrip("\n").partition("=")
            if k in wanted:
                hdr[k] = v
    missing = [k for k in ("run.begin_kernel_ns", "run.end_kernel_ns")
               if k not in hdr]
    if missing:
        raise SystemExit(
            "%s does not state its run bounds in the kernel clock (%s "
            "missing); a capture cannot record a window the victim does not "
            "declare" % (path, ", ".join(missing)))
    return {
        "record": os.path.basename(path),
        "metric": hdr.get("metric"),
        "context": hdr.get("context"),
        "kernel_clock": {
            "begin_ns": int(hdr["run.begin_kernel_ns"]),
            "end_ns": int(hdr["run.end_kernel_ns"]),
        },
        "counter_clock": {
            "domain_id": hdr.get("domain.id"),
            "counter_identity": hdr.get("domain.counter_identity"),
            "frequency_hz": (int(hdr["domain.frequency_hz"])
                             if "domain.frequency_hz" in hdr else None),
            "begin": (int(hdr["run.begin_wall"])
                      if "run.begin_wall" in hdr else None),
            "end": (int(hdr["run.end_wall"])
                    if "run.end_wall" in hdr else None),
        },
    }


def write_capture(cap, path):
    """A capture is written once, like the record it sits beside."""
    if os.path.exists(path):
        raise SystemExit("refusing to overwrite existing capture: %s" % path)
    mw = cap["measured_window"] or {}
    kc = mw.get("kernel_clock") or {}
    cc = mw.get("counter_clock") or {}
    with open(path, "w") as f:
        w = lambda k, v: f.write("# %s=%s\n" % (k, v))
        w("mcib_capture", cap["kind"])
        w("capture_version", cap["version"])
        w("source", cap["source"])
        w("clock", cap["clock"])
        w("clock_selected_at_open", int(bool(cap["clock_selected_at_open"])))
        w("cpu", cap["cpu"])
        w("events", " ".join(cap["events"]))
        w("ring_pages_per_event", cap["ring_pages_per_event"])
        w("ring_bytes_per_event", cap["ring_bytes_per_event"])
        w("collection.begin_kernel_ns", cap["collection_begin_kernel_ns"])
        w("collection.end_kernel_ns", cap["collection_end_kernel_ns"])
        w("measured_window.record", mw.get("record", ""))
        w("measured_window.metric", mw.get("metric", ""))
        w("measured_window.context", mw.get("context", ""))
        w("measured_window.begin_kernel_ns", kc.get("begin_ns", ""))
        w("measured_window.end_kernel_ns", kc.get("end_ns", ""))
        w("measured_window.domain_id", cc.get("domain_id", ""))
        w("measured_window.counter_identity", cc.get("counter_identity", ""))
        w("measured_window.frequency_hz", cc.get("frequency_hz", ""))
        w("measured_window.begin_wall", cc.get("begin", ""))
        w("measured_window.end_wall", cc.get("end", ""))
        w("dropped", cap["dropped"])
        w("samples", cap["samples"])
        for name in cap["events"]:
            w("samples.%s" % name, cap["samples_by_event"].get(name, 0))
        f.write("t_kernel_ns,cpu,event,pid,tid\n")
        for t, cpu, name, pid, tid in cap["rows"]:
            f.write("%d,%d,%s,%d,%d\n" % (t, cpu, name, pid, tid))
    return path


def collect_around(cpu, command, out_path, events=DEFAULT_EVENTS,
                   pages=DEFAULT_RING_PAGES, window_record=None,
                   stdout=None):
    """Collect while `command` runs, then write the capture.

    `stdout` is a path the command's output is redirected to, because a
    sequencer that captures a victim's output when it runs it directly must
    not stop doing so when it runs it under collection: two arrangements that
    differ in what they keep are two experiments."""
    import subprocess
    c = Collector(cpu, events, pages)
    c.start()
    out = open(stdout, "w") if stdout else None
    try:
        rc = subprocess.call(command, stdout=out)
    finally:
        if out:
            out.close()
        c.stop()
    window = (measured_window_from_record(window_record)
              if window_record else None)
    cap = c.capture(window)
    c.close()
    write_capture(cap, out_path)
    return rc, cap


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="collect kernel events for the trace tier",
        epilog="Separate the victim's own arguments with --, so that its "
               "flags are not read as this tool's: "
               "trace_collect.py --cpu 3 --out run.cap -- ./victim -c 3 -n 100000")
    ap.add_argument("--cpu", type=int, required=True,
                    help="the measured core")
    ap.add_argument("--out", required=True, help="capture path (written once)")
    ap.add_argument("--event", action="append", default=None,
                    help="tracepoint, subsystem:event; repeatable")
    ap.add_argument("--pages", type=int, default=DEFAULT_RING_PAGES,
                    help="ring pages per event")
    ap.add_argument("--window-record", default=None,
                    help="the victim record whose run bounds this capture "
                         "covers")
    ap.add_argument("--seconds", type=float, default=None,
                    help="collect for this long instead of around a command")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="run this, collecting while it runs; put -- before "
                         "it so its flags are not read as this tool's")
    args = ap.parse_args(argv[1:])
    events = tuple(args.event) if args.event else DEFAULT_EVENTS
    command = args.command[1:] if (args.command and args.command[0] == "--") \
        else args.command

    if command:
        rc, cap = collect_around(args.cpu, command, args.out, events,
                                 args.pages, args.window_record)
    elif args.seconds is not None:
        c = Collector(args.cpu, events, args.pages)
        c.start()
        time.sleep(args.seconds)
        c.stop()
        window = (measured_window_from_record(args.window_record)
                  if args.window_record else None)
        cap = c.capture(window)
        c.close()
        write_capture(cap, args.out)
        rc = 0
    else:
        ap.error("give a command to collect around, or --seconds")

    sys.stderr.write(
        "capture %s: %d samples, %d dropped, events %s\n"
        % (args.out, cap["samples"], cap["dropped"],
           ", ".join("%s=%d" % (k, v)
                     for k, v in sorted(cap["samples_by_event"].items()))))
    if cap["dropped"]:
        sys.stderr.write(
            "WARNING: this capture dropped %d events and analysis will "
            "refuse it; re-collect with a larger ring\n" % cap["dropped"])
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
