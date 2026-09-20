/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Counter ENCODING — raw PMU event codes.
 *
 * Raw event codes are per-microarchitecture. An unknown or unverified part is
 * an error at open, never a silent mismatch: a code that means one event on
 * one core can mean another, or nothing, on the next. Wrong counters produce
 * plausible numbers, which is worse than no numbers.
 *
 * The architectural events below (PMUv3 common events) are defined by the
 * architecture rather than by the implementation, which is why one entry
 * covers A72 and is a *candidate* for others — but a candidate still has to
 * be verified per part before it is added here.
 *
 * The set also carries two software events. They are not microarchitectural
 * and need no per-part verification, but they are what makes the backend's
 * claim to detect preemption true: without a context-switch and a migration
 * count there is no way to tell whether a measured segment ran to completion
 * on one CPU, and the per-run migration check the run discipline depends on
 * has nothing to read. A backend property that no counter delivers is worse
 * than one not declared, because the record carries the claim to whoever
 * reads it.
 */
#include <stdio.h>
#include <string.h>
#include <linux/perf_event.h>

#include "mcib_backend.h"

/* PMUv3 architectural event numbers. */
#define EV_L1D_CACHE_REFILL 0x03
#define EV_L2D_CACHE_REFILL 0x17
#define EV_BR_MIS_PRED      0x10
#define EV_CPU_CYCLES       0x11
#define EV_L1D_TLB_REFILL   0x05
#define EV_BR_PRED          0x12

static const mcib_counter_set_t set_arm_v3_default = {
    .name = "armv3-default",
    .n    = 8,
    .names = { "l1d_cache_refill", "l2d_cache_refill", "br_mis_pred",
               "cpu_cycles", "l1d_tlb_refill", "br_pred",
               "context_switches", "cpu_migrations" },
    .type  = { PERF_TYPE_RAW, PERF_TYPE_RAW, PERF_TYPE_RAW,
               PERF_TYPE_RAW, PERF_TYPE_RAW, PERF_TYPE_RAW,
               PERF_TYPE_SOFTWARE, PERF_TYPE_SOFTWARE },
    .config= { EV_L1D_CACHE_REFILL, EV_L2D_CACHE_REFILL, EV_BR_MIS_PRED,
               EV_CPU_CYCLES, EV_L1D_TLB_REFILL, EV_BR_PRED,
               PERF_COUNT_SW_CONTEXT_SWITCHES, PERF_COUNT_SW_CPU_MIGRATIONS },
};

/* A single-counter set.
 *
 * Its purpose is diagnostic: comparing a run under this set against the same
 * run under the full set separates cost that scales with the number of events
 * opened from cost that does not. The counter is cycles, because it is the
 * one every part implements and the one an execution-time denominator needs.
 *
 * It carries no software events, so a run under this set has no
 * context-switch or migration count and the backend cannot substantiate its
 * claim to detect preemption from this set alone. */
static const mcib_counter_set_t set_arm_v3_cycles = {
    .name  = "armv3-cycles",
    .n     = 1,
    .names = { "cpu_cycles" },
    .type  = { PERF_TYPE_RAW },
    .config= { EV_CPU_CYCLES },
};

/* A set for a metric whose subject is the scheduler rather than the memory
 * system.
 *
 * Four events, chosen against the counter budget rather than by habit: an
 * execution-time denominator, one last-level cache channel — the one a memory
 * aggressor perturbs, and the only hardware channel worth carrying when the
 * path under measurement is a kernel software path — and the two software
 * events without which the backend cannot substantiate its claim to detect
 * preemption, and without which a switch-counting metric cannot report the
 * quantity it is about.
 *
 * Every event past the first costs roughly ten times more outside the
 * snapshot than inside it, so four is a decision and not a default. */
static const mcib_counter_set_t set_arm_v3_sched = {
    .name  = "armv3-sched",
    .n     = 4,
    .names = { "cpu_cycles", "l2d_cache_refill",
               "context_switches", "cpu_migrations" },
    .type  = { PERF_TYPE_RAW, PERF_TYPE_RAW,
               PERF_TYPE_SOFTWARE, PERF_TYPE_SOFTWARE },
    .config= { EV_CPU_CYCLES, EV_L2D_CACHE_REFILL,
               PERF_COUNT_SW_CONTEXT_SWITCHES, PERF_COUNT_SW_CPU_MIGRATIONS },
};

/* A set for a metric whose subject is displacement rather than the memory
 * system.
 *
 * One discretionary event: cycles. It is the numerator of a utilisation
 * ratio — cycles retired against the interval's elapsed time — and that
 * ratio is how work displacing the measured context shows up when no cache
 * or branch channel moves. The prior campaign on this metric found every
 * hardware counter at 1.0x and the mechanism visible only in the utilisation
 * figure and in trace events, so adding cache or branch events here would
 * buy nothing and cost per-event time.
 *
 * The other two are the backend's qualification counters, not a campaign
 * slot — and on this metric they are also the direct evidence: an interrupt
 * displacing the measured context is a context switch or a migration. */
static const mcib_counter_set_t set_arm_v3_util = {
    .name  = "armv3-util",
    .n     = 3,
    .names = { "cpu_cycles", "context_switches", "cpu_migrations" },
    .type  = { PERF_TYPE_RAW, PERF_TYPE_SOFTWARE, PERF_TYPE_SOFTWARE },
    .config= { EV_CPU_CYCLES,
               PERF_COUNT_SW_CONTEXT_SWITCHES, PERF_COUNT_SW_CPU_MIGRATIONS },
};

/* Verified-for list. Adding a part here requires checking each code against
 * that part's technical reference manual. Presence here means someone
 * checked; absence means nobody has, not that the codes are wrong. */
static const char *verified_parts[] = { "cortex-a72", NULL };

/* MIDR_EL1 part numbers. Read via /proc/cpuinfo rather than the register so
 * this works from EL0 without a trap. */
const char *mcib_platform_id(void)
{
    static char id[32];
    if (id[0]) return id;

    FILE *f = fopen("/proc/cpuinfo", "re");
    if (!f) return NULL;

    char line[256];
    unsigned part = 0;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "CPU part : %x", &part) == 1) break;
        if (sscanf(line, "CPU part: %x", &part) == 1) break;
    }
    fclose(f);

    switch (part) {
    case 0xd08: snprintf(id, sizeof id, "cortex-a72"); break;
    case 0xd0b: snprintf(id, sizeof id, "cortex-a76"); break;
    case 0xd03: snprintf(id, sizeof id, "cortex-a53"); break;
    case 0xd07: snprintf(id, sizeof id, "cortex-a57"); break;
    default:    return NULL;
    }
    return id;
}

const mcib_counter_set_t *mcib_counter_set_lookup(const char *name,
                                                  mcib_error_t *err)
{
    const char *plat = mcib_platform_id();
    if (!plat) {
        if (err) {
            snprintf(err->msg, sizeof err->msg,
                     "probe: unrecognised CPU implementation — no verified "
                     "counter map. Add the part to events_arm.c after "
                     "checking each event code against its reference manual.");
            err->code = -1;
        }
        return NULL;
    }

    int verified = 0;
    for (const char **p = verified_parts; *p; p++)
        if (strcmp(*p, plat) == 0) { verified = 1; break; }

    if (!verified) {
        if (err) {
            snprintf(err->msg, sizeof err->msg,
                     "probe: counter map not verified for %s. The default set "
                     "uses PMUv3 architectural events, which are likely but "
                     "NOT confirmed for this part. Verify against "
                     "the TRM and add it to verified_parts before measuring.",
                     plat);
            err->code = -1;
        }
        return NULL;
    }

    if (!name || strcmp(name, set_arm_v3_default.name) == 0)
        return &set_arm_v3_default;

    if (strcmp(name, set_arm_v3_cycles.name) == 0)
        return &set_arm_v3_cycles;

    if (strcmp(name, set_arm_v3_sched.name) == 0)
        return &set_arm_v3_sched;

    if (strcmp(name, set_arm_v3_util.name) == 0)
        return &set_arm_v3_util;

    if (err) {
        snprintf(err->msg, sizeof err->msg,
                 "probe: unknown counter set '%s'", name);
        err->code = -1;
    }
    return NULL;
}
