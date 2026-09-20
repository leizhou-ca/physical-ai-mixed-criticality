/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Linux counter backend — counters bound to a CPU rather than to a context.
 *
 * Opened with pid=-1 and an explicit cpu, so the counters are programmed once
 * on that core and left running. Nothing is saved or restored when the kernel
 * switches away from the measuring context, which is the entire point: the
 * per-switch cost that dominates the per-thread backend on a blocking
 * workload does not arise.
 *
 * WHAT IT COUNTS IS DIFFERENT, and that is not a side effect to be minimised.
 * A per-thread counter stops when the thread blocks; this one keeps counting
 * whatever the core runs — the measuring context, the kernel entered on its
 * behalf, interrupts taken on that core, and any other thread scheduled
 * there. On an isolated core the last of those is usually nothing, but
 * "usually" is not a property, which is why this backend declares that it
 * does NOT follow the thread and leans on preemption detection instead.
 *
 * Measured on a two-thread metric sharing one core: a per-CPU segment read
 * 2.3x the per-thread one, and almost all of that difference was the peer
 * thread, not kernel work on the measured context's behalf. Attribution from
 * such a segment would assign the peer's behaviour to the context being
 * measured. This backend is therefore NOT the default: use it where the
 * measured context is alone on its core — an application control loop is the
 * shape that fits — and prefer the per-thread backend where a peer shares it.
 *
 * The cpu is not configured: it is read from the calling context, which the
 * initialisation order guarantees is already pinned. A backend that took a
 * cpu number could be given one the caller does not run on, and would then
 * count a core nobody is measuring.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdarg.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <linux/perf_event.h>

#include "mcib_backend.h"

struct percpu_ctx {
    int      fd[MCIB_MAX_COUNTERS];
    int      group_fd;
    uint32_t n;
    int      cpu;
    uint64_t buf[1 + MCIB_MAX_COUNTERS];
};

static long perf_open(struct perf_event_attr *a, pid_t pid, int cpu,
                      int group_fd, unsigned long flags)
{ return syscall(__NR_perf_event_open, a, pid, cpu, group_fd, flags); }

static void set_err(mcib_error_t *err, int code, const char *fmt, ...)
{
    if (!err) return;
    va_list ap; va_start(ap, fmt);
    vsnprintf(err->msg, sizeof err->msg, fmt, ap);
    va_end(ap);
    err->code = code;
}

static int be_open(const mcib_counter_set_t *set, void **out, mcib_error_t *err)
{
    int cpu = sched_getcpu();
    if (cpu < 0) {
        set_err(err, errno, "probe: cannot determine the calling context's "
                            "CPU, which a per-CPU backend must bind to: %s",
                strerror(errno));
        return -1;
    }
    cpu_set_t aff;
    CPU_ZERO(&aff);
    if (sched_getaffinity(0, sizeof aff, &aff) == 0 && CPU_COUNT(&aff) != 1) {
        set_err(err, EINVAL,
                "probe: the calling context is not pinned to one CPU (it may "
                "run on %d), so a per-CPU counter would not follow it. Pin "
                "before opening the probe.", CPU_COUNT(&aff));
        return -1;
    }

    struct percpu_ctx *c = calloc(1, sizeof *c);
    if (!c) { set_err(err, ENOMEM, "probe: out of memory"); return -1; }
    c->group_fd = -1;
    c->n = set->n;
    c->cpu = cpu;

    for (uint32_t i = 0; i < set->n; i++) {
        struct perf_event_attr a;
        memset(&a, 0, sizeof a);
        a.size           = sizeof a;
        a.type           = set->type[i];
        a.config         = set->config[i];
        a.disabled       = (i == 0);
        a.exclude_kernel = 0;   /* the kernel path is the subject */
        a.exclude_hv     = 1;
        a.read_format    = PERF_FORMAT_GROUP;
        a.inherit        = 0;   /* not permitted with a cpu-bound event */

        long fd = perf_open(&a, -1, cpu, c->group_fd, 0);
        if (fd < 0) {
            int e = errno;
            set_err(err, e,
                    "probe: perf_event_open failed for counter %u (%s) bound "
                    "to CPU%d: %s. A CPU-bound counter needs privilege that a "
                    "context-bound one does not; perf_event_paranoid must "
                    "permit it. The probe does not fall back to another "
                    "backend — that would silently change what is counted.",
                    i, set->names[i], cpu, strerror(e));
            for (uint32_t k = 0; k < i; k++) close(c->fd[k]);
            free(c);
            return -1;
        }
        c->fd[i] = (int)fd;
        if (i == 0) c->group_fd = (int)fd;
    }

    ioctl(c->group_fd, PERF_EVENT_IOC_RESET,  PERF_IOC_FLAG_GROUP);
    ioctl(c->group_fd, PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP);
    *out = c;
    return 0;
}

static int be_snapshot(void *ctx, uint64_t *out)
{
    struct percpu_ctx *c = ctx;
    ssize_t want = (ssize_t)((1 + c->n) * sizeof(uint64_t));
    ssize_t got  = read(c->group_fd, c->buf, (size_t)want);
    if (got != want) return -1;
    for (uint32_t i = 0; i < c->n; i++) out[i] = c->buf[1 + i];
    return 0;
}

static void be_close(void *ctx)
{
    struct percpu_ctx *c = ctx;
    if (!c) return;
    ioctl(c->group_fd, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
    for (uint32_t i = 0; i < c->n; i++) close(c->fd[i]);
    free(c);
}

const mcib_counter_backend_t mcib_backend_linux_percpu = {
    .name       = "linux-perf-percpu",
    /* FOLLOWS_THREAD is absent and its absence is the declaration that
     * matters: a delta from this backend covers the CORE's execution over the
     * segment, not the context's. Qualification therefore rests on
     * PREEMPTION_DETECTABLE, which the set's context-switch and migration
     * events deliver. */
    .properties = MCIB_BE_SYSCALL_IN_PATH
                | MCIB_BE_PREEMPTION_DETECTABLE,
    .open       = be_open,
    .snapshot   = be_snapshot,
    .close      = be_close,
};
