/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * MCIB probe.
 *
 * Hot path: no allocation, no locking, no file I/O, no coordination with any
 * other context. Everything is pre-allocated and pre-faulted at open, and the
 * timestamp reads are fenced so neither the compiler nor the processor can
 * move surrounding work across the measured boundary.
 *
 * Open verifies its preconditions rather than applying them, and measures
 * two things about itself that cannot be assumed: which time source is
 * cheapest on this platform, and what the instrument's own per-event cost is.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef NDEBUG
#include <assert.h>
#endif

#include "mcib_probe.h"
#include "mcib_backend.h"
#include "mcib_time.h"

/* ----------------------------------------------------------- barriers */

/* Compiler barrier: stops the compiler moving memory operations across this
 * point. Costs nothing at run time. */
#define MCIB_COMPILER_BARRIER() __asm__ volatile("" ::: "memory")

/* Processor barrier around a timing boundary. On a weakly-ordered machine the
 * core may complete work out of order, so without this the interval measured
 * is not the interval bracketed — silently, per sample.
 *
 * Cost is part of the instrument: it is constant per event, so it stays
 * common-mode between compared conditions, and it is measured at open and
 * written into the record rather than assumed negligible. */
#if defined(__aarch64__)
#  define MCIB_FENCE()  __asm__ volatile("dsb ish; isb" ::: "memory")
#  define MCIB_RELEASE() __asm__ volatile("dmb ishst" ::: "memory")
#elif defined(__x86_64__) || defined(__i386__)
#  define MCIB_FENCE()  __asm__ volatile("mfence; lfence" ::: "memory")
#  define MCIB_RELEASE() MCIB_COMPILER_BARRIER()   /* x86 stores are ordered */
#else
#  define MCIB_FENCE()  __sync_synchronize()
#  define MCIB_RELEASE() __sync_synchronize()
#endif

/* -------------------------------------------------------------- clocks */

/* Kernel time base. CLOCK_MONOTONIC is the base the kernel's trace facility
 * exposes as its "mono" clock and the base eBPF's timestamp helper returns,
 * so events from all three join without conversion.
 *
 * The free-running reading is NOT chosen here — see mcib_time.h. Which source
 * is cheapest is a platform fact that has to be measured, and on a part where
 * the counter register is trapped both candidates can be expensive. */
static inline uint64_t kernel_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t ticks_to_ns(uint64_t ticks, uint64_t hz)
{
    if (hz == 0) return 0;
    /* Split so a large tick count cannot overflow the multiply. */
    return (ticks / hz) * 1000000000ull + ((ticks % hz) * 1000000000ull) / hz;
}

/* ---------------------------------------------------------- calibration */

/* How many back-to-back marks the instrument records against itself at open.
 *
 * The figure that matters is a distribution, not a single number: a median
 * says what an event costs and a 99th percentile says what the worst event
 * costs, and the second is what a tail analysis has to subtract. 512 events
 * puts five samples above the 99th percentile, so that percentile is a
 * measurement rather than an extrapolation, and costs 512 slots of scratch —
 * about 53 KB — which is allocated and pre-faulted with everything else. */
#define MCIB_CAL_EVENTS 512u

/* -------------------------------------------------------------- faults */

typedef struct {
    uint64_t minor;
    uint64_t major;
    bool     valid;
} fault_snap_t;

static void fault_read(fault_snap_t *s)
{
    struct rusage ru;
    if (getrusage(RUSAGE_THREAD, &ru) == 0) {
        s->minor = (uint64_t)ru.ru_minflt;
        s->major = (uint64_t)ru.ru_majflt;
        s->valid = true;
    } else {
        /* Not zero-with-a-straight-face: an unreadable count is recorded as
         * unreadable, so a later subtraction cannot turn it into an apparent
         * clean run or wrap below zero into an enormous one. */
        s->minor = s->major = 0;
        s->valid = false;
    }
}

static bool fault_delta(const fault_snap_t *a, const fault_snap_t *b,
                        uint64_t *dmin, uint64_t *dmaj)
{
    if (!a->valid || !b->valid) { *dmin = 0; *dmaj = 0; return false; }
    *dmin = (b->minor >= a->minor) ? b->minor - a->minor : 0;
    *dmaj = (b->major >= a->major) ? b->major - a->major : 0;
    return true;
}

/* -------------------------------------------------------------- probe */

struct mcib_probe {
    mcib_probe_config_t cfg;
    char                metric[64];
    char                context[64];
    char                exec_context[64];

    mcib_event_t       *ev;
    uint64_t            cap;
    uint64_t            n;
    uint64_t            seq;
    bool                overflowed;

    const mcib_counter_backend_t *be;
    const mcib_counter_set_t     *set;
    void                         *bectx;
    bool                          counters;
    /* True when `bectx` is BORROWED from a core-scoped handle rather than
     * opened by this probe. A borrowed handle is never closed here: it
     * outlives every probe that referenced it, and closing it from one of
     * them would stop the accumulator the others are still reading. */
    bool                          counters_borrowed;
    int                           core_cpu;

    /* Time source, chosen by measurement at open. */
    const mcib_time_source_t *tsrc;
    uint64_t            freq;
    uint64_t            tsrc_cost_ns;
    uint64_t            tsrc_cost_worst_ns;
    char                tsrc_detail[96];

    /* Self-calibration: the instrument's own per-event inflation. */
    mcib_event_t       *cal;
    uint64_t           *cal_d;
    uint64_t            cal_p50_ns;
    uint64_t            cal_p99_ns;

    uint64_t            warmup_left;
    uint64_t            warmup_done;

    /* Reentrancy is prohibited, not made safe: one instance, one context.
     * Builds with assertions enabled check; measurement builds do not. */
    pthread_t           owner;

    /* The measuring context's own scheduling state, read at open. Distinct
     * from the orchestrator's isolation facts: this is what the thread that
     * actually took the samples was running as. */
    int                 sched_policy;
    int                 sched_priority;
    int                 sched_cpu;

    /* Measurement-loop boundaries, in both clock domains. */
    bool                have_bounds;
    uint64_t            run_begin_wall, run_begin_kns;
    uint64_t            run_end_wall,   run_end_kns;

    fault_snap_t        flt_open, flt_close, flt_begin, flt_end;
};

/* Pre-fault this context's stack.
 *
 * Locking memory maps what is already mapped. A thread's stack below the
 * current frame has never been touched, so those pages are not present when
 * it runs — the first deep call in the measured path then faults, allocates,
 * and that fault lands inside a sample. Touching every page here maps them,
 * and the process-wide lock the caller took covers them as they appear.
 *
 * Must run in EACH measured context: every thread has its own stack. */
#define MCIB_STACK_PREFAULT_BYTES (64u * 1024u)

__attribute__((noinline))
static void prefault_stack(void)
{
    volatile unsigned char buf[MCIB_STACK_PREFAULT_BYTES];
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) page = 4096;
    for (size_t i = 0; i < sizeof buf; i += (size_t)page)
        buf[i] = 0;
    buf[sizeof buf - 1] = 0;
    MCIB_COMPILER_BARRIER();   /* the writes must not be optimised away */
}

static void perr(mcib_error_t *e, int code, const char *fmt, ...)
{
    if (!e) return;
    va_list ap; va_start(ap, fmt);
    vsnprintf(e->msg, sizeof e->msg, fmt, ap);
    va_end(ap);
    e->code = code;
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

/* ------------------------------------------------------------ hot path */

static inline void record(mcib_probe_t *p, uint32_t point_id, uint64_t token,
                          bool boundary)
{
#ifndef NDEBUG
    /* One instance, one context. Corruption here is silent otherwise. */
    assert(pthread_equal(p->owner, pthread_self()));
#endif
    /* Both branches below are bounded by construction: a decrementing count
     * and a capacity comparison. Neither can loop. */
    if (__builtin_expect(p->warmup_left > 0, 0)) {
        p->warmup_left--;
        p->warmup_done++;
        return;
    }
    if (__builtin_expect(p->n >= p->cap, 0)) { p->overflowed = true; return; }

    mcib_event_t *e = &p->ev[p->n];
    uint16_t flags = 0;

    /* Nothing from before this point may drift past it. */
    MCIB_FENCE();

    if (p->counters) {
        if (p->be->snapshot(p->bectx, e->counters) == 0) {
            flags |= MCIB_EV_HAS_COUNTERS;
            /* A segment is untrustworthy only when the backend can NEITHER
             * follow the context off-CPU NOR tell that it was preempted.
             * Either property alone is enough to know the delta covers this
             * context's own execution. */
            if (!(p->be->properties & MCIB_BE_FOLLOWS_THREAD) &&
                !(p->be->properties & MCIB_BE_PREEMPTION_DETECTABLE))
                flags |= MCIB_EV_UNQUALIFIED;
        } else {
            flags |= MCIB_EV_READ_FAILED;
        }
    }

    bool want_kts =
        (p->cfg.kts_policy == MCIB_KTS_EVERY_EVENT) ||
        (p->cfg.kts_policy == MCIB_KTS_BOUNDARY && boundary);
    if (want_kts) {
        e->t_kernel_ns = kernel_ns();
        flags |= MCIB_EV_HAS_KERNEL_TS;
    }

    /* Counter snapshot and timestamps form one logical instant; their order
     * is fixed here rather than left to the compiler. The free-running
     * reading is taken last, as close to the measured boundary as the
     * ordering rules allow, so the instrument's share of the interval is as
     * small as it can be. What remains of it is measured at open. */
    MCIB_COMPILER_BARRIER();
    e->t_wall    = p->tsrc->read();

    e->seq       = p->seq++;
    e->point_id  = point_id;
    e->token     = token;
    e->domain_id = p->cfg.domain_id;
    e->flags     = flags;

    /* Publish: the slot's fields are written before the count that makes it
     * readable. Without the release a reader could see a counted-but-unfilled
     * slot on a weakly-ordered machine. */
    MCIB_RELEASE();
    p->n++;

    /* Nothing after this point may drift back into the measured section. */
    MCIB_FENCE();
}

void mcib_probe_mark(mcib_probe_t *p, uint32_t id, uint64_t tok)
{ record(p, id, tok, false); }

void mcib_probe_mark_boundary(mcib_probe_t *p, uint32_t id, uint64_t tok)
{ record(p, id, tok, true); }

void mcib_probe_enter(mcib_probe_t *p, uint32_t id, uint64_t tok)
{ record(p, id, tok, true); }

void mcib_probe_exit(mcib_probe_t *p, uint32_t id, uint64_t tok)
{ record(p, id, tok, true); }

/* --------------------------------------------------------- calibration */

/* Records MCIB_CAL_EVENTS back-to-back marks with nothing at all between
 * them, into scratch storage, and takes the resulting null interval's median
 * and 99th percentile. That interval IS the instrument's per-event inflation:
 * the counter snapshot, the kernel timestamp, the barriers, the buffer write,
 * and any assertion still compiled in.
 *
 * It goes through the real hot path, on purpose. A calibration that used a
 * simplified copy would measure something the run does not execute.
 *
 * Calibration events must not reach the event stream, so the buffer, counts
 * and sequence are swapped out and restored around it. */
static int calibrate(mcib_probe_t *p)
{
    mcib_event_t *save_ev   = p->ev;
    uint64_t      save_cap  = p->cap;
    uint64_t      save_n    = p->n;
    uint64_t      save_seq  = p->seq;
    uint64_t      save_warm = p->warmup_left;

    p->ev = p->cal; p->cap = MCIB_CAL_EVENTS;
    p->n  = 0;      p->seq = 0; p->warmup_left = 0;

    for (uint32_t i = 0; i < MCIB_CAL_EVENTS; i++)
        record(p, 0, 0, true);

    uint64_t got = p->n;

    p->ev = save_ev; p->cap = save_cap;
    p->n  = save_n;  p->seq = save_seq; p->warmup_left = save_warm;

    if (got < 2) return -1;

    uint64_t m = got - 1;
    for (uint64_t i = 0; i < m; i++)
        p->cal_d[i] = p->cal[i + 1].t_wall - p->cal[i].t_wall;

    qsort(p->cal_d, (size_t)m, sizeof p->cal_d[0], cmp_u64);

    /* Nearest-rank, so both figures are samples that were observed. */
    uint64_t r99 = (uint64_t)((0.99 * (double)m) + 0.5);
    if (r99 == 0) r99 = 1;
    if (r99 > m)  r99 = m;

    p->cal_p50_ns = ticks_to_ns(p->cal_d[m / 2],  p->freq);
    p->cal_p99_ns = ticks_to_ns(p->cal_d[r99 - 1], p->freq);
    return 0;
}

/* ----------------------------------------------------- precondition: lock */

/* Read back whether this process's memory is locked.
 *
 * There is no getmlockall(), so the read-back is the kernel's own accounting:
 * VmLck in this process's status is the number of locked kilobytes. A process
 * that locked current and future mappings reports a non-zero figure; one
 * whose lock was refused reports zero. This is the same
 * read-the-actual-state rule every other precondition in the harness follows,
 * rather than trusting that a call was made.
 *
 * Returns 0 when the state could be determined, -1 when it could not. */
static int memory_lock_state(bool *locked, unsigned long *kb)
{
    FILE *f = fopen("/proc/self/status", "re");
    if (!f) return -1;

    char line[256];
    int  seen = 0;
    unsigned long v = 0;
    while (fgets(line, sizeof line, f)) {
        if (sscanf(line, "VmLck: %lu kB", &v) == 1) { seen = 1; break; }
    }
    fclose(f);
    if (!seen) return -1;

    *kb = v;
    *locked = (v > 0);
    return 0;
}

static void read_sched_state(mcib_probe_t *p)
{
    struct sched_param sp;
    memset(&sp, 0, sizeof sp);
    p->sched_policy   = sched_getscheduler(0);
    p->sched_priority = (sched_getparam(0, &sp) == 0) ? sp.sched_priority : -1;
    p->sched_cpu      = sched_getcpu();
}

static const char *policy_name(int pol)
{
    switch (pol) {
    case SCHED_FIFO:  return "SCHED_FIFO";
    case SCHED_RR:    return "SCHED_RR";
    case SCHED_OTHER: return "SCHED_OTHER";
    case SCHED_BATCH: return "SCHED_BATCH";
    case SCHED_IDLE:  return "SCHED_IDLE";
    default:          return "unknown";
    }
}

/* -------------------------------------------------------------- open */

/* ---------------------------------------------- core-scoped counter handle
 *
 * One accumulator for one core, referenced by the probes of a core-owned
 * interval. The whole of its reason for existing is that a difference
 * between two readings requires the two readings to share an origin: two
 * handles on the same core are two accumulators, and their difference
 * carries the gap between their opens, which was measured here at 47.7
 * million cycles.
 *
 * It is deliberately thin. It owns the backend context and nothing else; the
 * probes that reference it do the reading, and each writes its own record.
 * Nothing is shared in the hot path but the file descriptor the backend
 * already reads through, so no synchronisation is added to a measured
 * section — which is the constraint that rules out every arrangement where
 * one context reads on another's behalf. */

struct mcib_core_counters {
    const mcib_counter_backend_t *be;
    const mcib_counter_set_t     *set;
    void                         *ctx;
    int                           cpu;
};

int mcib_core_counters_open(const char *counter_set, const char *backend,
                            mcib_core_counters_t **out, mcib_error_t *err)
{
    if (!out) { perr(err, EINVAL, "probe: no handle to write"); return -1; }
    *out = NULL;

    const mcib_counter_backend_t *be = mcib_backend_by_name(backend, err);
    if (!be) return -1;                       /* message already set */

    /* A thread-bound handle shared between two contexts would follow
     * whichever thread opened it and stop counting whenever that thread was
     * off-CPU — which, on the metric this exists for, is half the time and
     * exactly the half being measured. Refused rather than accepted with a
     * caveat. */
    if (be->properties & MCIB_BE_FOLLOWS_THREAD) {
        perr(err, EINVAL,
             "probe: backend '%s' follows the thread, so a handle shared "
             "between two contexts would count only the thread that opened "
             "it. A core-scoped handle needs a CPU-bound backend.", be->name);
        return -1;
    }

    int cpu = sched_getcpu();
    if (cpu < 0) {
        perr(err, errno, "probe: cannot determine the calling context's CPU, "
                         "which a core-scoped handle must bind to: %s",
             strerror(errno));
        return -1;
    }

    const mcib_counter_set_t *set = mcib_counter_set_lookup(counter_set, err);
    if (!set) return -1;                      /* message already set */

    mcib_core_counters_t *h = calloc(1, sizeof *h);
    if (!h) { perr(err, ENOMEM, "probe: out of memory"); return -1; }
    h->be = be;
    h->set = set;
    h->cpu = cpu;
    /* The backend checks that the caller is pinned to exactly one CPU and
     * refuses otherwise, which is the half of the ordering rule that can be
     * verified from here. That the handle exists before either context marks
     * is the caller's, and cannot be checked from inside. */
    if (be->open(set, &h->ctx, err) != 0) {
        free(h);
        return -1;
    }
    *out = h;
    return 0;
}

void mcib_core_counters_close(mcib_core_counters_t *h)
{
    if (!h) return;
    h->be->close(h->ctx);
    free(h);
}

int mcib_core_counters_cpu(const mcib_core_counters_t *h)
{
    return h ? h->cpu : -1;
}


mcib_probe_t *mcib_probe_open(const mcib_probe_config_t *cfg, mcib_error_t *err)
{
    if (err) { err->code = 0; err->msg[0] = 0; }
    if (!cfg || cfg->capacity == 0) {
        perr(err, EINVAL, "probe: capacity must be non-zero");
        return NULL;
    }

    /* Precondition, checked before anything is built: the process's memory
     * must already be locked. The probe does not lock it — mlockall is
     * process-wide, and the probe owns no process-wide state — and it does
     * not warn and continue, because a hot path that can fault produces
     * latency spikes unrelated to the measurement and a console warning is
     * not a property of the record. */
    bool locked = false;
    unsigned long lock_kb = 0;
    if (memory_lock_state(&locked, &lock_kb) != 0) {
        perr(err, -1, "probe: cannot verify that memory is locked (no "
                      "readable VmLck in this process's status). The probe "
                      "will not measure without that guarantee.");
        return NULL;
    }
    if (!locked) {
        perr(err, -1,
             "probe: memory is not locked (VmLck is 0 kB). The caller must "
             "call mlockall(MCL_CURRENT | MCL_FUTURE) before opening the "
             "probe — the lock is process-wide, so it belongs to whoever "
             "owns the process, not to the probe. Without it a page fault "
             "can land inside a measured section and appear as latency.");
        return NULL;
    }

    mcib_probe_t *p = calloc(1, sizeof *p);
    if (!p) { perr(err, ENOMEM, "probe: out of memory"); return NULL; }

    p->cfg = *cfg;
    snprintf(p->metric,  sizeof p->metric,  "%s", cfg->metric       ? cfg->metric       : "unnamed");
    snprintf(p->context, sizeof p->context, "%s", cfg->context_name ? cfg->context_name : "ctx0");
    /* Not defaulted to "host": a guest cannot tell from inside that it is
     * one, so an unstated context is recorded as unstated rather than having
     * a wrong answer asserted on the caller's behalf. */
    snprintf(p->exec_context, sizeof p->exec_context, "%s",
             cfg->execution_context ? cfg->execution_context : "unspecified");
    p->cap         = cfg->capacity;
    p->warmup_left = cfg->warmup_events;
    p->owner       = pthread_self();
    p->sched_policy = -1;
    p->sched_priority = -1;
    p->sched_cpu = -1;

    p->ev    = calloc(p->cap, sizeof *p->ev);
    p->cal   = calloc(MCIB_CAL_EVENTS, sizeof *p->cal);
    p->cal_d = calloc(MCIB_CAL_EVENTS, sizeof *p->cal_d);
    if (!p->ev || !p->cal || !p->cal_d) {
        perr(err, ENOMEM, "probe: cannot allocate %llu event slots plus "
                          "calibration scratch",
             (unsigned long long)p->cap);
        free(p->cal_d); free(p->cal); free(p->ev); free(p);
        return NULL;
    }

    /* Pre-fault the stack of THIS context before anything else touches it. */
    prefault_stack();

    /* Touch and retain every buffer: a large allocation arrives as untouched
     * pages, and touching without retaining is not pre-faulting. */
    memset(p->ev,    0, p->cap * sizeof *p->ev);
    memset(p->cal,   0, MCIB_CAL_EVENTS * sizeof *p->cal);
    memset(p->cal_d, 0, MCIB_CAL_EVENTS * sizeof *p->cal_d);

    read_sched_state(p);

    /* Which time source is cheapest is measured, never assumed, and the
     * frequency is read only from whichever source that measurement chose. */
    mcib_time_choice_t tc;
    if (mcib_time_select(&tc, err) != 0) {
        free(p->cal_d); free(p->cal); free(p->ev); free(p);
        return NULL;
    }
    p->tsrc               = tc.src;
    p->freq               = tc.frequency_hz;
    p->tsrc_cost_ns       = tc.cost_ns;
    p->tsrc_cost_worst_ns = tc.cost_ns_worst;
    snprintf(p->tsrc_detail, sizeof p->tsrc_detail, "%s", tc.detail);

    p->core_cpu = -1;
    if (cfg->counters && cfg->core_counters) {
        /* A core-scoped handle supplies the backend, the set and the open
         * accumulator. Naming a different set or backend beside it is a
         * contradiction, and it is refused rather than resolved silently one
         * way: a record claiming one counter set while reading another is
         * exactly the class of defect the record format exists to prevent. */
        const struct mcib_core_counters *h = cfg->core_counters;
        if (cfg->counter_set &&
            strcmp(cfg->counter_set, h->set->name) != 0) {
            perr(err, EINVAL,
                 "probe: the core-scoped handle counts set '%s' but this "
                 "context asked for '%s'; one accumulator cannot be two sets",
                 h->set->name, cfg->counter_set);
            free(p->cal_d); free(p->cal); free(p->ev); free(p);
            return NULL;
        }
        if (cfg->counter_backend &&
            strcmp(cfg->counter_backend, h->be->name) != 0) {
            perr(err, EINVAL,
                 "probe: the core-scoped handle uses backend '%s' but this "
                 "context asked for '%s'", h->be->name, cfg->counter_backend);
            free(p->cal_d); free(p->cal); free(p->ev); free(p);
            return NULL;
        }
        p->be    = h->be;
        p->set   = h->set;
        p->bectx = h->ctx;
        p->counters = true;
        p->counters_borrowed = true;
        p->core_cpu = h->cpu;
    } else if (cfg->counters) {
        p->be  = mcib_backend_by_name(cfg->counter_backend, err);
        if (!p->be) {                        /* message already set */
            free(p->cal_d); free(p->cal); free(p->ev); free(p);
            return NULL;
        }
        p->set = mcib_counter_set_lookup(cfg->counter_set, err);
        if (!p->set) {                       /* message already set */
            free(p->cal_d); free(p->cal); free(p->ev); free(p);
            return NULL;
        }
        if (p->be->open(p->set, &p->bectx, err) != 0) {
            free(p->cal_d); free(p->cal); free(p->ev); free(p);
            return NULL;   /* fail atomically, no downgrade */
        }
        p->counters = true;
    }

    /* Warm the counter read path once, outside measurement, so the first hot
     * call does not pay any one-off cost. */
    if (p->counters) {
        uint64_t scratch[MCIB_MAX_COUNTERS];
        (void)p->be->snapshot(p->bectx, scratch);
    }

    /* Calibrate last, so it runs through the hot path exactly as the run
     * will: chosen time source, opened counters, whatever assertions this
     * build carries. It also warms every one-off cost on that path — the
     * first kernel-timestamp call, the first touch of the event slots — so
     * they do not land on the first recorded event. */
    if (calibrate(p) != 0) {
        perr(err, -1, "probe: self-calibration produced no usable interval");
        if (p->counters && !p->counters_borrowed) p->be->close(p->bectx);
        free(p->cal_d); free(p->cal); free(p->ev); free(p);
        return NULL;
    }

    fault_read(&p->flt_open);
    return p;
}

/* -------------------------------------------------------- run bounds */

void mcib_probe_run_begin(mcib_probe_t *p)
{
    if (!p) return;
    fault_read(&p->flt_begin);
    p->run_begin_kns  = kernel_ns();
    p->run_begin_wall = p->tsrc->read();
    p->have_bounds    = true;
}

void mcib_probe_run_end(mcib_probe_t *p)
{
    if (!p || !p->have_bounds) return;
    p->run_end_wall = p->tsrc->read();
    p->run_end_kns  = kernel_ns();
    fault_read(&p->flt_end);
}

/* ---------------------------------------------------------- inspection */

uint64_t mcib_probe_count(const mcib_probe_t *p) { return p ? p->n : 0; }
bool mcib_probe_overflowed(const mcib_probe_t *p) { return p && p->overflowed; }

/* Which pair of snapshots bounds the measurement. Explicit run bounds when
 * the caller marked the loop; otherwise open to close, which is wider and
 * includes teardown — the record says which was used. */
static bool fault_window(const mcib_probe_t *p, uint64_t *dmin, uint64_t *dmaj)
{
    if (p->have_bounds && p->flt_end.valid)
        return fault_delta(&p->flt_begin, &p->flt_end, dmin, dmaj);
    return fault_delta(&p->flt_open, &p->flt_close, dmin, dmaj);
}

bool mcib_probe_faulted(const mcib_probe_t *p, uint64_t *minor, uint64_t *major,
                        bool *valid)
{
    if (!p) return false;

    uint64_t dmin = 0, dmaj = 0;
    bool ok;

    if (p->have_bounds && p->flt_end.valid) {
        ok = fault_delta(&p->flt_begin, &p->flt_end, &dmin, &dmaj);
    } else if (p->flt_close.valid) {
        ok = fault_delta(&p->flt_open, &p->flt_close, &dmin, &dmaj);
    } else {
        /* Asked before close and without explicit bounds: close the window
         * here so the caller gets a live answer rather than nothing. */
        fault_snap_t now;
        fault_read(&now);
        ok = fault_delta(&p->flt_open, &now, &dmin, &dmaj);
    }

    if (minor) *minor = dmin;
    if (major) *major = dmaj;
    if (valid) *valid = ok;

    /* A check that could not be performed is not a clean result. */
    if (!ok) return true;
    return (dmin != 0) || (dmaj != 0);
}

const mcib_event_t *mcib_probe_events(const mcib_probe_t *p, uint64_t *n)
{
    if (n) *n = p ? p->n : 0;
    return p ? p->ev : NULL;
}

uint32_t mcib_probe_counter_names(const mcib_probe_t *p,
                                  const char *names[MCIB_MAX_COUNTERS])
{
    if (!p || !p->set) return 0;
    for (uint32_t i = 0; i < p->set->n; i++) names[i] = p->set->names[i];
    return p->set->n;
}

void mcib_probe_instrument(const mcib_probe_t *p, mcib_instrument_t *out)
{
    if (!p || !out) return;
    out->time_source          = p->tsrc ? p->tsrc->name : "none";
    out->time_source_cost_ns  = p->tsrc_cost_ns;
    out->frequency_hz         = p->freq;
    out->null_interval_p50_ns = p->cal_p50_ns;
    out->null_interval_p99_ns = p->cal_p99_ns;
    out->calibration_events   = MCIB_CAL_EVENTS;
}

void mcib_probe_domain(const mcib_probe_t *p, mcib_clock_domain_t *out)
{
    if (!p || !out) return;
    out->domain_id        = p->cfg.domain_id;
    out->counter_identity = p->tsrc ? p->tsrc->name : "none";
    out->frequency_hz     = p->freq;
    out->execution_context= p->exec_context;
    out->kernel_clock     = "CLOCK_MONOTONIC";
}

/* -------------------------------------------------------------- record */

static void write_header(FILE *f, const mcib_probe_t *p)
{
    mcib_clock_domain_t d;
    mcib_probe_domain(p, &d);

    uint64_t dmin = 0, dmaj = 0;
    bool fok = fault_window(p, &dmin, &dmaj);
    bool suspect = (!fok) || dmin != 0 || dmaj != 0;

    /* Self-describing header: a record without its provenance cannot be
     * interpreted by anyone who was not present when it was taken. */
    fprintf(f, "# mcib_record_version=2\n");
    fprintf(f, "# metric=%s\n", p->metric);
    fprintf(f, "# context=%s\n", p->context);

    /* Clock domain, as a block rather than as scattered fields. Origin
     * identity and its offset evidence are absent by construction: two
     * contexts can read the same counter at the same frequency and still not
     * share a time base, and the offset is not readable from inside. It is
     * established by measuring a common reference from both sides, which one
     * context cannot do alone. */
    fprintf(f, "# domain.id=%u\n", d.domain_id);
    fprintf(f, "# domain.counter_identity=%s\n", d.counter_identity);
    fprintf(f, "# domain.frequency_hz=%llu\n",
            (unsigned long long)d.frequency_hz);
    fprintf(f, "# domain.execution_context=%s\n", d.execution_context);
    fprintf(f, "# domain.kernel_clock=%s\n", d.kernel_clock);
    fprintf(f, "# domain.origin_identity=none\n");
    fprintf(f, "# domain.offset_evidence=none\n");

    /* Instrument: what the probe measured about itself before measuring
     * anything else. */
    fprintf(f, "# instrument.time_source=%s\n", p->tsrc->name);
    fprintf(f, "# instrument.time_source_cost_ns=%llu\n",
            (unsigned long long)p->tsrc_cost_ns);
    fprintf(f, "# instrument.time_source_cost_worst_ns=%llu\n",
            (unsigned long long)p->tsrc_cost_worst_ns);
    fprintf(f, "# instrument.time_source_candidates=%s\n", p->tsrc_detail);
    fprintf(f, "# instrument.time_source_ceiling_ns=%u\n",
            MCIB_TIME_SOURCE_MAX_COST_NS);
    fprintf(f, "# instrument.calibration_events=%u\n", MCIB_CAL_EVENTS);
    fprintf(f, "# instrument.null_interval_p50_ns=%llu\n",
            (unsigned long long)p->cal_p50_ns);
    fprintf(f, "# instrument.null_interval_p99_ns=%llu\n",
            (unsigned long long)p->cal_p99_ns);
    fprintf(f, "# instrument.assertions=%s\n",
#ifdef NDEBUG
            "off"
#else
            "on"
#endif
            );

    /* The measuring context's own scheduling state. */
    fprintf(f, "# context.sched_policy=%s\n", policy_name(p->sched_policy));
    fprintf(f, "# context.sched_priority=%d\n", p->sched_priority);
    fprintf(f, "# context.cpu=%d\n", p->sched_cpu);

    fprintf(f, "# kts_policy=%d\n", (int)p->cfg.kts_policy);
    fprintf(f, "# warmup_events=%llu\n", (unsigned long long)p->warmup_done);

    /* Run boundaries, in both clock domains. */
    fprintf(f, "# run.bounds=%s\n", p->have_bounds ? "explicit" : "inferred");
    if (p->have_bounds) {
        fprintf(f, "# run.begin_wall=%llu\n",
                (unsigned long long)p->run_begin_wall);
        fprintf(f, "# run.end_wall=%llu\n",
                (unsigned long long)p->run_end_wall);
        fprintf(f, "# run.begin_kernel_ns=%llu\n",
                (unsigned long long)p->run_begin_kns);
        fprintf(f, "# run.end_kernel_ns=%llu\n",
                (unsigned long long)p->run_end_kns);
        fprintf(f, "# run.duration_ns=%llu\n",
                (unsigned long long)(p->run_end_kns > p->run_begin_kns
                                     ? p->run_end_kns - p->run_begin_kns : 0));
    } else if (p->n > 0) {
        fprintf(f, "# run.begin_wall=%llu\n",
                (unsigned long long)p->ev[0].t_wall);
        fprintf(f, "# run.end_wall=%llu\n",
                (unsigned long long)p->ev[p->n - 1].t_wall);
        fprintf(f, "# run.begin_kernel_ns=%llu\n",
                (unsigned long long)p->ev[0].t_kernel_ns);
        fprintf(f, "# run.end_kernel_ns=%llu\n",
                (unsigned long long)p->ev[p->n - 1].t_kernel_ns);
    }
    fprintf(f, "# run.events=%llu\n", (unsigned long long)p->n);
    fprintf(f, "# run.overflowed=%d\n", p->overflowed ? 1 : 0);

    /* Fault accounting, with its window and its verdict — not only its
     * number. Memory was verifiably locked at open, so an increase here has
     * one remaining cause: a pre-fault that did not reach deep enough. */
    fprintf(f, "# faults.window=%s\n",
            (p->have_bounds && p->flt_end.valid) ? "measurement-loop"
                                                 : "open-to-close");
    fprintf(f, "# faults.readable=%d\n", fok ? 1 : 0);
    fprintf(f, "# faults.minor=%llu\n", (unsigned long long)dmin);
    fprintf(f, "# faults.major=%llu\n", (unsigned long long)dmaj);
    fprintf(f, "# faults.verdict=%s\n", suspect ? "suspect" : "clean");

    fprintf(f, "# memory_locked=1\n");
    fprintf(f, "# stack_prefault_bytes=%u\n",
            (unsigned)MCIB_STACK_PREFAULT_BYTES);
    fprintf(f, "# platform=%s\n",
            mcib_platform_id() ? mcib_platform_id() : "unknown");

    if (p->counters) {
        fprintf(f, "# counters.enabled=1\n");
        fprintf(f, "# counters.backend=%s\n", p->be->name);
        fprintf(f, "# counters.backend_properties=0x%x\n", p->be->properties);
        fprintf(f, "# counters.follows_thread=%d\n",
                (p->be->properties & MCIB_BE_FOLLOWS_THREAD) ? 1 : 0);
        fprintf(f, "# counters.syscall_in_path=%d\n",
                (p->be->properties & MCIB_BE_SYSCALL_IN_PATH) ? 1 : 0);
        fprintf(f, "# counters.preemption_detectable=%d\n",
                (p->be->properties & MCIB_BE_PREEMPTION_DETECTABLE) ? 1 : 0);
        fprintf(f, "# counters.set=%s\n", p->set->name);
        /* Whose accumulator the delta came from. A consumer differencing two
         * contexts' snapshots must know they read ONE accumulator, and this
         * is where it is told — without having to infer it from the backend
         * name, which says what is counted and not who holds the handle. */
        fprintf(f, "# counters.scope=%s\n",
                p->counters_borrowed ? "core" : "context");
        fprintf(f, "# counters.core_cpu=%d\n", p->core_cpu);
    } else {
        fprintf(f, "# counters.enabled=0\n");
        fprintf(f, "# counters.backend=none\n");
        fprintf(f, "# counters.scope=none\n");
        fprintf(f, "# counters.core_cpu=-1\n");
    }

    /* Flag legend: a record that cannot be read without the library's header
     * file is not self-describing. */
    fprintf(f, "# flags.bit0=0x%04x has_kernel_ts\n", MCIB_EV_HAS_KERNEL_TS);
    fprintf(f, "# flags.bit1=0x%04x has_counters\n",  MCIB_EV_HAS_COUNTERS);
    fprintf(f, "# flags.bit2=0x%04x unqualified"
               " (counter segment not trustworthy)\n", MCIB_EV_UNQUALIFIED);
    fprintf(f, "# flags.bit3=0x%04x read_failed"
               " (counter columns are zeros, not counts)\n",
            MCIB_EV_READ_FAILED);
}

int mcib_probe_close(mcib_probe_t *p, const char *path, mcib_error_t *err)
{
    if (!p) return -1;
    int rc = 0;

    fault_read(&p->flt_close);
    MCIB_RELEASE();

    if (!path) {
        /* A run that writes no record has produced nothing. Treating that as
         * success is how a measurement campaign loses an afternoon. */
        perr(err, EINVAL, "probe: a record path is required; a run with no "
                          "record is not a successful run");
        rc = -1;
    } else {
        /* Exclusive create: the record is written once and never rewritten,
         * so an existing file is an error rather than something to truncate.
         * Overwriting is the one destructive act this library could perform
         * on a caller's data, and it refuses it. */
        int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
        if (fd < 0) {
            if (errno == EEXIST)
                perr(err, EEXIST,
                     "probe: record '%s' already exists and will not be "
                     "overwritten. Records are written once and named per "
                     "run; choose another path or move the existing one.",
                     path);
            else
                perr(err, errno, "probe: cannot create record '%s': %s",
                     path, strerror(errno));
            rc = -1;
        } else {
            FILE *f = fdopen(fd, "w");
            if (!f) {
                perr(err, errno, "probe: cannot open record '%s': %s",
                     path, strerror(errno));
                close(fd);
                rc = -1;
            } else {
                write_header(f, p);

                fprintf(f, "context_id,seq,point_id,token,t_wall,"
                           "t_kernel_ns,domain_id,flags");
                if (p->counters)
                    for (uint32_t i = 0; i < p->set->n; i++)
                        fprintf(f, ",%s", p->set->names[i]);
                fputc('\n', f);

                for (uint64_t i = 0; i < p->n; i++) {
                    const mcib_event_t *e = &p->ev[i];
                    fprintf(f, "%s,%llu,%u,%llu,%llu,%llu,%u,0x%04x",
                            p->context,
                            (unsigned long long)e->seq, e->point_id,
                            (unsigned long long)e->token,
                            (unsigned long long)e->t_wall,
                            (unsigned long long)e->t_kernel_ns,
                            e->domain_id, e->flags);
                    if (p->counters)
                        for (uint32_t k = 0; k < p->set->n; k++)
                            fprintf(f, ",%llu",
                                    (unsigned long long)e->counters[k]);
                    fputc('\n', f);
                }
                if (fclose(f) != 0) {
                    perr(err, errno, "probe: record write failed: %s",
                         strerror(errno));
                    rc = -1;
                }
            }
        }
    }

    if (p->counters && !p->counters_borrowed) p->be->close(p->bectx);
    free(p->cal_d);
    free(p->cal);
    free(p->ev);
    free(p);
    return rc;
}
