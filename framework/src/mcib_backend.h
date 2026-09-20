/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Counter backend interface.
 *
 * perf_event_open is a Linux service. Zephyr and bare-metal read the PMU
 * registers directly and provide no thread-following semantics at all, so
 * counter ACCESS is a separate axis from counter ENCODING. A backend
 * declares what it can and cannot promise; the record carries that
 * declaration, and the probe marks segments unqualified where the backend
 * cannot vouch for them.
 */
#ifndef MCIB_BACKEND_H
#define MCIB_BACKEND_H

#include <stdint.h>
#include "mcib_probe.h"

/* Backend capability declaration. */
enum {
    /* Counting pauses while the context is off-CPU, so a delta covers only
     * that context's own execution. True on Linux per-thread events.
     * False on an RTOS reading the PMU registers directly. */
    MCIB_BE_FOLLOWS_THREAD        = 1u << 0,
    /* A snapshot costs a syscall. */
    MCIB_BE_SYSCALL_IN_PATH       = 1u << 1,
    /* The backend can tell whether the context was preempted inside a
     * measured segment (e.g. via context-switch / migration counters). */
    MCIB_BE_PREEMPTION_DETECTABLE = 1u << 2,
};

typedef struct mcib_counter_set {
    const char *name;
    uint32_t    n;
    const char *names[MCIB_MAX_COUNTERS];
    uint32_t    type  [MCIB_MAX_COUNTERS];   /* backend-specific */
    uint64_t    config[MCIB_MAX_COUNTERS];   /* backend-specific */
} mcib_counter_set_t;

typedef struct {
    const char *name;
    uint32_t    properties;

    /* Open counters for the CALLING context. Returns 0 on success. */
    int  (*open)    (const mcib_counter_set_t *set, void **ctx, mcib_error_t *err);
    /* Hot path. Writes set->n values into out. Returns 0, or -1 if the read
     * failed — in which case out is left untouched and the caller marks the
     * event MCIB_EV_READ_FAILED. Never fabricates a value. */
    int  (*snapshot)(void *ctx, uint64_t *out);
    void (*close)   (void *ctx);
} mcib_counter_backend_t;

/* The default backend for this build: counters bound to the calling context.
 * Every shipped measurement uses this one. */
const mcib_counter_backend_t *mcib_backend(void);

/* Select a backend by name. NULL or an unknown name yields the default, and
 * *err says which happened. Backends differ in WHAT they count, not only in
 * how fast they read it, so the choice belongs at the call site and in the
 * record rather than in a build flag. */
const mcib_counter_backend_t *mcib_backend_by_name(const char *name,
                                                   mcib_error_t *err);

/* Platform event map — counter ENCODING, keyed by implementation.
 * Raw event codes are per-microarchitecture; an unknown platform is an
 * error at open, not a silent mismatch. */
const mcib_counter_set_t *mcib_counter_set_lookup(const char *name,
                                                  mcib_error_t *err);

/* Identify the running implementation, e.g. "cortex-a72". NULL if unknown. */
const char *mcib_platform_id(void);

#endif /* MCIB_BACKEND_H */
