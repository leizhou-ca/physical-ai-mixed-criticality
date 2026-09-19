/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Time source candidates and the measurement that picks between them.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "mcib_time.h"

/* Keeps the candidate reads from being optimised out of the timing loop. */
static volatile uint64_t time_sink;

/* Common reference for the selection measurement. CLOCK_MONOTONIC is the base
 * the kernel trace facility exposes and the one the probe stamps events with,
 * so it is present wherever the probe is. It is used here only to divide a
 * burst; its own cost is amortised across the burst length. */
static uint64_t ref_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* --------------------------------------------- candidate: arch counter */

#if defined(__aarch64__)
static uint64_t arch_read(void)
{
    uint64_t v;
    /* isb before the read: the counter value must not be satisfied out of
     * order from work issued earlier. */
    __asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(v) :: "memory");
    return v;
}
static uint64_t arch_freq(void)
{
    uint64_t v;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(v));
    return v;
}
#define MCIB_HAVE_ARCH_COUNTER 1
#endif

/* ------------------------------------------ candidate: monotonic raw */

static uint64_t raw_read(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}
static uint64_t raw_freq(void) { return 1000000000ull; }

/* ------------------------------------------------------------ table */

static const mcib_time_source_t sources[] = {
#ifdef MCIB_HAVE_ARCH_COUNTER
    { "arch-counter",        arch_read, arch_freq },
#endif
    { "clock-monotonic-raw", raw_read,  raw_freq  },
};
#define N_SOURCES ((unsigned)(sizeof sources / sizeof sources[0]))

/* ------------------------------------------------------------ timing */

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

/* Median per-read cost of one source, in nanoseconds. Never returns 0: a
 * source that appears free has been optimised away or the reference is too
 * coarse, and either way the figure must not be trusted as "free". */
static void measure_source(const mcib_time_source_t *s,
                           uint64_t *median_ns, uint64_t *worst_ns)
{
    uint64_t cost[MCIB_TIME_BURSTS];

    /* One discarded burst: first-call resolution, i-cache and branch
     * predictor. Warming here is what keeps the figure a steady-state cost
     * rather than a first-call one. */
    for (unsigned k = 0; k < MCIB_TIME_BURST_READS; k++)
        time_sink = s->read();

    for (unsigned b = 0; b < MCIB_TIME_BURSTS; b++) {
        uint64_t t0 = ref_ns();
        for (unsigned k = 0; k < MCIB_TIME_BURST_READS; k++)
            time_sink = s->read();
        uint64_t t1 = ref_ns();
        cost[b] = (t1 > t0) ? (t1 - t0) / MCIB_TIME_BURST_READS : 0;
    }

    qsort(cost, MCIB_TIME_BURSTS, sizeof cost[0], cmp_u64);
    *median_ns = cost[MCIB_TIME_BURSTS / 2];
    *worst_ns  = cost[MCIB_TIME_BURSTS - 1];
}

int mcib_time_select(mcib_time_choice_t *out, mcib_error_t *err)
{
    uint64_t med[N_SOURCES], wor[N_SOURCES];
    unsigned best = 0;
    size_t   pos  = 0;

    memset(out, 0, sizeof *out);
    out->n_candidates = N_SOURCES;

    for (unsigned i = 0; i < N_SOURCES; i++) {
        measure_source(&sources[i], &med[i], &wor[i]);
        if (med[i] < med[best]) best = i;
        if (pos < sizeof out->detail) {
            int w = snprintf(out->detail + pos, sizeof out->detail - pos,
                             "%s%s=%llu", i ? " " : "", sources[i].name,
                             (unsigned long long)med[i]);
            if (w > 0) pos += (size_t)w;
            if (pos > sizeof out->detail) pos = sizeof out->detail;
        }
    }

    /* The ceiling is checked before the frequency is read, and the frequency
     * is read only for the winner. On a part where the counter register is
     * trapped, the frequency register is trapped with it — so it must not be
     * touched until that source has been shown to be usable. */
    if (med[best] > MCIB_TIME_SOURCE_MAX_COST_NS) {
        if (err) {
            snprintf(err->msg, sizeof err->msg,
                     "probe: no usable time source. Cheapest %s at %llu "
                     "ns/read, over the %u ns ceiling. Candidates: %s. "
                     "A read this costly is trapped or emulated, not a "
                     "fast path.",
                     sources[best].name, (unsigned long long)med[best],
                     MCIB_TIME_SOURCE_MAX_COST_NS, out->detail);
            err->code = -1;
        }
        return -1;
    }

    uint64_t hz = sources[best].frequency();
    if (hz == 0) {
        if (err) {
            snprintf(err->msg, sizeof err->msg,
                     "probe: time source '%s' reports a frequency of zero; "
                     "intervals cannot be converted to time.",
                     sources[best].name);
            err->code = -1;
        }
        return -1;
    }

    out->src           = &sources[best];
    out->frequency_hz  = hz;
    out->cost_ns       = med[best];
    out->cost_ns_worst = wor[best];
    return 0;
}
