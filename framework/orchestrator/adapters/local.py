#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# The local adapter: the domain is the machine this process runs on.
#
# THE INTERFACE IS FIVE VERBS, and everything above is written against them:
#
#   describe   what this domain can provide — counter backend, tracing,
#              clock identity, privilege. A domain that cannot do something
#              says so, and the limitation is reported rather than
#              discovered halfway through a session.
#   deploy     place a binary and its data
#   start/stop run a role with its arguments
#   collect    retrieve records and captures
#
# `describe` is the one that matters. It is the same property-declaration
# pattern the counter backend already uses: a capability that is declared but
# not delivered is worse than one that is absent, because the claim
# propagates to whoever reads the result.
import os
import shutil
import subprocess


class AdapterError(Exception):
    pass


class LocalAdapter(object):
    name = "local"

    def __init__(self, workdir=None):
        self.workdir = workdir or os.getcwd()

    # -- reading the domain --------------------------------------------------

    def read_text(self, path, default=None):
        """Read a file in the domain. Returns `default` where it does not
        exist or cannot be read, because most callers here are asking a
        question the absence of the file answers."""
        try:
            with open(path) as f:
                return f.read().strip()
        except (IOError, OSError):
            return default

    def glob(self, pattern):
        import glob as _glob
        return sorted(_glob.glob(pattern))

    def exists(self, path):
        return os.path.exists(path)

    def which(self, binary):
        return shutil.which(binary)

    def run(self, argv, timeout=None, check=False, env=None):
        """Run a command in the domain and return (rc, stdout, stderr)."""
        merged = dict(os.environ)
        merged.update(env or {})
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout, env=merged)
        except FileNotFoundError as e:
            raise AdapterError("%s: %s" % (argv[0], e))
        except subprocess.TimeoutExpired:
            raise AdapterError("%s timed out after %ss" % (argv[0], timeout))
        if check and p.returncode != 0:
            raise AdapterError("%s exited %d: %s"
                               % (argv[0], p.returncode, p.stderr.strip()))
        return p.returncode, p.stdout, p.stderr

    def write_text(self, path, value):
        """Write a sysfs or procfs setting. Returns (ok, error)."""
        try:
            with open(path, "w") as f:
                f.write(value)
            return True, None
        except (IOError, OSError) as e:
            return False, str(e)

    def free_mb(self, path="/"):
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)

    # -- the five verbs ------------------------------------------------------

    def describe(self):
        """What this domain can provide.

        Every field is a fact read from the domain, never a default. A caller
        uses this to decide what it may claim, so a guess here becomes a
        false claim downstream."""
        cpus = self.read_text("/sys/devices/system/cpu/isolated", "") or ""
        tracing = None
        for p in ("/sys/kernel/tracing/events", "/sys/kernel/debug/tracing/events"):
            if os.path.isdir(p):
                tracing = os.path.dirname(p)
                break
        paranoid = self.read_text("/proc/sys/kernel/perf_event_paranoid")
        return {
            "adapter": self.name,
            "kernel_release": os.uname().release,
            "kernel_version": os.uname().version,
            "machine": os.uname().machine,
            "privileged": (os.geteuid() == 0),
            "isolated_cpus": cpus,
            "online_cpus": self.read_text(
                "/sys/devices/system/cpu/online", ""),
            "tracing": {
                "available": tracing is not None,
                "path": tracing,
                "source": "perf-tracepoints" if tracing else None,
                "note": None if tracing else
                        "no tracepoint definitions are readable in this "
                        "domain, so the kernel-event tier is unavailable "
                        "here; counter attribution is unaffected",
            },
            "counters": {
                "perf_event_paranoid": (int(paranoid)
                                        if paranoid and
                                        paranoid.lstrip("-").isdigit()
                                        else None),
                "perf_event_open": os.path.exists("/proc/sys/kernel/perf_event_paranoid"),
            },
            "clock": {
                "cpufreq": os.path.isdir(
                    "/sys/devices/system/cpu/cpu0/cpufreq"),
            },
            "cgroup2": os.path.exists("/sys/fs/cgroup/cgroup.controllers"),
        }

    def deploy(self, src, dest):
        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copy2(src, dest)
        return dest

    def start(self, argv, cwd=None, env=None, stdout=None):
        merged = dict(os.environ)
        merged.update(env or {})
        out = open(stdout, "w") if stdout else subprocess.PIPE
        p = subprocess.Popen(argv, cwd=cwd or self.workdir, env=merged,
                             stdout=out, stderr=subprocess.STDOUT)
        p._mcib_stdout_file = out if stdout else None
        return p

    def stop(self, handle, timeout=10):
        if handle.poll() is None:
            handle.terminate()
            try:
                handle.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                handle.kill()
                handle.wait()
        f = getattr(handle, "_mcib_stdout_file", None)
        if f:
            f.close()
        return handle.returncode

    def collect(self, paths, into):
        """Retrieve produced files. Local is already in place; the verb
        exists so that nothing above this file knows that."""
        os.makedirs(into, exist_ok=True)
        got = []
        for p in paths:
            if not os.path.exists(p):
                continue
            target = os.path.join(into, os.path.basename(p))
            if os.path.abspath(p) != os.path.abspath(target):
                shutil.copy2(p, target)
            got.append(target)
        return got
