/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Linux counter backend.
 *
 * Per-thread grouped counters (pid=0, cpu=-1). Because the events follow the
 * calling thread, counting pauses while that thread is off-CPU, so a delta
 * covers only its own execution. That property is declared below as
 * FOLLOWS_THREAD, and it is what allows a counter delta to be attributed to
 * one context's execution segment.
 *
 * Measured on a Cortex-A72 under a 6.12 PREEMPT_RT kernel: with a sleep
 * inside the measured section, wall time grew 75x while the cycle delta grew
 * 1.7%. The property holds on this backend. It does NOT hold generally — an
 * RTOS reading the PMU registers directly has no such mechanism, and its
 * backend must declare the absence so segments are marked unqualified.
 *
 * The group mixes hardware and software events deliberately. Software events
 * are always schedulable, so adding them to a hardware-led group costs no
 * counter slot, and they are what makes the declared ability to detect
 * preemption real: the context-switch and migration counts are the evidence
 * that a segment ran to completion on one CPU. One read() still covers the
 * whole group, so the hot path pays for one syscall however many events the
 * set carries.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <linux/perf_event.h>

#include "mcib_backend.h"

struct perf_ctx {
    int      fd[MCIB_MAX_COUNTERS];
    int      group_fd;
    uint32_t n;
    /* read buffer: nr, then one value per member (PERF_FORMAT_GROUP) */
    uint64_t buf[1 + MCIB_MAX_COUNTERS];
};

static long perf_open(struct perf_event_attr *a, pid_t pid, int cpu,
                      int group_fd, unsigned long flags)
{
    return syscall(__NR_perf_event_open, a, pid, cpu, group_fd, flags);
}

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
    struct perf_ctx *c = calloc(1, sizeof *c);
    if (!c) { set_err(err, ENOMEM, "probe: out of memory"); return -1; }

    c->group_fd = -1;
    c->n = set->n;

    for (uint32_t i = 0; i < set->n; i++) {
        struct perf_event_attr a;
        memset(&a, 0, sizeof a);
        a.size           = sizeof a;
        a.type           = set->type[i];
        a.config         = set->config[i];
        a.disabled       = (i == 0);
        a.exclude_kernel = 0;   /* kernel path is the subject of measurement */
        a.exclude_hv     = 1;
        a.read_format    = PERF_FORMAT_GROUP;
        a.inherit        = 0;   /* this context only */

        /* pid=0: follow the calling thread. cpu=-1: on whatever CPU it runs.
         * This pairing is what gives FOLLOWS_THREAD. */
        long fd = perf_open(&a, 0, -1, c->group_fd, 0);
        if (fd < 0) {
            int e = errno;
            set_err(err, e,
                    "probe: perf_event_open failed for counter %u (%s): %s. "
                    "Counters were requested and are unavailable; the probe "
                    "does not silently continue without them.",
                    i, set->names[i], strerror(e));
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

/* Hot path: one read() for the whole group. */
static int be_snapshot(void *ctx, uint64_t *out)
{
    struct perf_ctx *c = ctx;
    ssize_t want = (ssize_t)((1 + c->n) * sizeof(uint64_t));
    ssize_t got  = read(c->group_fd, c->buf, (size_t)want);
    if (got != want) return -1;            /* caller marks READ_FAILED */
    for (uint32_t i = 0; i < c->n; i++) out[i] = c->buf[1 + i];
    return 0;
}

static void be_close(void *ctx)
{
    struct perf_ctx *c = ctx;
    if (!c) return;
    ioctl(c->group_fd, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
    for (uint32_t i = 0; i < c->n; i++) close(c->fd[i]);
    free(c);
}

static const mcib_counter_backend_t linux_perf = {
    .name       = "linux-perf",
    /* Every property here is delivered by something in the build:
     * FOLLOWS_THREAD by the pid=0/cpu=-1 pairing above, SYSCALL_IN_PATH by
     * the read() in the snapshot, PREEMPTION_DETECTABLE by the
     * context-switch and migration counters in the default set. */
    .properties = MCIB_BE_FOLLOWS_THREAD
                | MCIB_BE_SYSCALL_IN_PATH
                | MCIB_BE_PREEMPTION_DETECTABLE,
    .open       = be_open,
    .snapshot   = be_snapshot,
    .close      = be_close,
};

const mcib_counter_backend_t *mcib_backend(void) { return &linux_perf; }

/* Backends other than the default, declared where they are defined. */
extern const mcib_counter_backend_t mcib_backend_linux_percpu;

static const mcib_counter_backend_t *const registry[] = {
    &linux_perf,
    &mcib_backend_linux_percpu,
};

const mcib_counter_backend_t *mcib_backend_by_name(const char *name,
                                                   mcib_error_t *err)
{
    if (!name || !*name) return &linux_perf;
    for (size_t i = 0; i < sizeof registry / sizeof registry[0]; i++)
        if (strcmp(registry[i]->name, name) == 0) return registry[i];
    /* Not silently defaulted: a caller that asked for a backend counting
     * something else must not be given the default and told nothing. */
    set_err(err, EINVAL, "probe: unknown counter backend '%s'", name);
    return NULL;
}
