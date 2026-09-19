/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Shared victim scaffolding.
 *
 * Not part of the probe library: these are obligations a victim has, not
 * things the probe does for it. They live in one header so that three victims
 * cannot drift into three different answers to the same question — which is
 * how the defects this file exists to fix arose in the first place.
 */
#ifndef MCIB_VICTIM_COMMON_H
#define MCIB_VICTIM_COMMON_H

#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "mcib_probe.h"

/* ------------------------------------------------- cross-context status
 *
 * A failure flag written by one measured context and read by another is
 * shared state, and a plain int is not a synchronisation primitive: the
 * reader may see the flag set and the message still empty, or never see the
 * flag at all. This is outside the hot path — it is written once, on a
 * failure path, and read after the join — so an atomic costs nothing that
 * matters and removes the race.
 *
 * The message is published BEFORE the flag, with release/acquire ordering, so
 * a reader that sees the flag sees a complete message. */
typedef struct {
    atomic_int failed;
    char       msg[MCIB_ERR_LEN];
} victim_status_t;

static inline void victim_status_init(victim_status_t *s)
{
    atomic_init(&s->failed, 0);
    s->msg[0] = 0;
}

static inline void victim_fail(victim_status_t *s, const char *msg)
{
    /* First writer wins: the first failure is the one that explains the rest. */
    int expected = 0;
    if (atomic_compare_exchange_strong_explicit(&s->failed, &expected, 1,
                                                memory_order_relaxed,
                                                memory_order_relaxed)) {
        snprintf(s->msg, sizeof s->msg, "%s", msg ? msg : "unspecified failure");
        atomic_store_explicit(&s->failed, 2, memory_order_release);
    }
}

static inline bool victim_failed(const victim_status_t *s, const char **msg)
{
    int v = atomic_load_explicit(&s->failed, memory_order_acquire);
    if (v == 0) return false;
    if (msg) *msg = (v == 2 && s->msg[0]) ? s->msg : "a measured context failed";
    return true;
}

/* --------------------------------------------------- instrument, at startup
 *
 * Printed before the run, not after parsing a record: an operator who can see
 * that the instrument costs more than the thing being measured can stop the
 * run rather than discover it afterwards. */
static inline void victim_print_instrument(const mcib_probe_t *p,
                                           const char *context)
{
    mcib_instrument_t in;
    mcib_probe_instrument(p, &in);
    printf("instrument[%s]: time source %s at %llu ns/read, %llu Hz; "
           "null interval over %u events p50 %llu ns p99 %llu ns\n",
           context, in.time_source,
           (unsigned long long)in.time_source_cost_ns,
           (unsigned long long)in.frequency_hz,
           in.calibration_events,
           (unsigned long long)in.null_interval_p50_ns,
           (unsigned long long)in.null_interval_p99_ns);
    fflush(stdout);
}

/* ------------------------------------------------------------- short run
 *
 * A run that recorded fewer events than it was asked for did not measure what
 * was requested, and leaving that to be inferred from an event count is how a
 * truncated dataset gets analysed as a complete one. */
static inline bool victim_short_run(uint64_t got, uint64_t expected,
                                    const char *context)
{
    if (got >= expected) return false;
    fprintf(stderr,
            "victim: SHORT RUN in context '%s' — recorded %llu events of the "
            "%llu expected. The run did not complete and its record is not a "
            "measurement of what was asked for\n",
            context, (unsigned long long)got, (unsigned long long)expected);
    return true;
}

#endif /* MCIB_VICTIM_COMMON_H */
