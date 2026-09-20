/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * MCIB measurement probe — public interface.
 *
 * The primitive is an EVENT, not an interval. One event is recorded in one
 * execution context. Intervals are constructed after the run, outside this
 * library.
 *
 * Why not a paired begin/end: an interval may span two execution contexts —
 * a context switch between two threads, a message crossing between two
 * processes, a request crossing between a guest and its host. In those cases
 * no single context owns the interval, and a paired API could only work by
 * coordinating between contexts inside the measured section. Coordination
 * there costs an unbounded, load-dependent amount of time, which is exactly
 * what a latency instrument must not add.
 *
 * Contracts the caller depends on:
 *   - one probe instance per execution context; reentrancy is prohibited
 *   - the hot path allocates nothing, locks nothing, and performs no I/O
 *   - a counter delta covers one context's own execution segment; no delta
 *     is ever claimed across contexts
 *   - open() fails atomically, and *err is set ONLY on failure: there is no
 *     warn-and-continue path and no partially initialised probe
 *   - open() verifies its preconditions rather than applying them. It does
 *     not lock memory: mlockall is process-wide and belongs to whoever owns
 *     the process. The caller locks; the probe checks and refuses to run if
 *     the lock is absent.
 */
#ifndef MCIB_PROBE_H
#define MCIB_PROBE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MCIB_MAX_COUNTERS 8
#define MCIB_ERR_LEN      320

/* ---------------------------------------------------------------- errors */

typedef struct {
    int  code;                    /* errno where applicable, else -1 */
    char msg[MCIB_ERR_LEN];
} mcib_error_t;

/* --------------------------------------------------------------- events */

/* Kept POD and fixed-size: the hot path writes one of these into a
 * pre-allocated slot and does nothing else. */
typedef struct {
    uint64_t seq;                 /* per-context monotonic sequence        */
    uint64_t t_wall;              /* free-running counter reading          */
    uint64_t t_kernel_ns;         /* kernel time base, 0 if not taken      */
    uint64_t token;               /* correlation identity, 0 if none       */
    uint32_t point_id;            /* which instrumented point              */
    uint16_t domain_id;           /* index into the record's clock table   */
    uint16_t flags;               /* MCIB_EV_*                             */
    uint64_t counters[MCIB_MAX_COUNTERS];
} mcib_event_t;

enum {
    MCIB_EV_HAS_KERNEL_TS = 1u << 0,  /* t_kernel_ns is valid              */
    MCIB_EV_HAS_COUNTERS  = 1u << 1,  /* counters[] is valid               */
    MCIB_EV_UNQUALIFIED   = 1u << 2,  /* counter segment may not be trusted:
                                       * the backend can neither follow the
                                       * context off-CPU nor detect that it
                                       * was preempted                     */
    MCIB_EV_READ_FAILED   = 1u << 3,  /* a counter read failed. No value was
                                       * computed or substituted, so the
                                       * slot still holds the zeros it was
                                       * pre-faulted with. The counter
                                       * columns of such a row carry no
                                       * information and must be discarded
                                       * on this flag, not read as counts. */
};

/* The record header prints this legend, one line per bit, so a record can be
 * read without this file. Keep the two in step. */

/* ------------------------------------------------- kernel-timestamp policy
 *
 * Each event can carry two readings: a free-running counter (cheap, monotonic,
 * what intervals are measured with) and the kernel time base (what the kernel
 * trace facility stamps its events with, and therefore the only way to align
 * probe events with kernel events).
 *
 * Measured cost of the kernel reading on a 1.5 GHz Cortex-A72: ~56 ns, about
 * 0.7% of a 15 us interval. Per-event is affordable there. The policy exists
 * because that ratio is platform-dependent, and because a run that never
 * joins against a kernel trace has no use for the second reading. Whichever
 * policy is chosen, it is recorded with the data. */
typedef enum {
    MCIB_KTS_EVERY_EVENT = 0,     /* every event carries a kernel timestamp */
    MCIB_KTS_BOUNDARY    = 1,     /* only events marked as boundaries       */
    MCIB_KTS_NEVER       = 2,     /* no trace alignment wanted              */
} mcib_kts_policy_t;

/* ---------------------------------------------------------------- config */

typedef struct {
    const char       *metric;         /* stable metric identifier          */
    const char       *context_name;   /* this context's name in the record */
    /* Which execution context this is — "host", "guest-1", an RTOS
     * instance name. It cannot be discovered from inside: a guest reading
     * the counter sees the same registers the host does. The caller states
     * it, and a caller that does not say leaves it unspecified rather than
     * having a wrong answer asserted on its behalf. */
    const char       *execution_context;
    uint32_t          warmup_events;  /* discarded; count is recorded      */
    uint32_t          capacity;       /* pre-allocated event slots         */
    bool              counters;       /* request counter capture           */
    const char       *counter_set;    /* NULL => platform default set      */
    /* Which counter-access backend to use. NULL selects the platform
     * default, which is the per-thread one every measurement has used.
     * Naming another is a characterisation act: a backend that binds
     * counters to a CPU rather than to a context counts different things,
     * and the record carries the name and the declared properties so a
     * reader can tell which was used. */
    const char       *counter_backend;
    mcib_kts_policy_t kts_policy;
    uint16_t          domain_id;      /* clock domain this context reads   */
} mcib_probe_config_t;

typedef struct mcib_probe mcib_probe_t;

/* ------------------------------------------------------------- lifecycle */

/* Call from the measuring context, AFTER affinity and scheduling policy are
 * set: per-thread counters follow the calling thread, so opening them earlier
 * binds them to a context whose scheduling then changes.
 *
 * The caller must already have locked the process's memory
 * (mlockall(MCL_CURRENT | MCL_FUTURE)). open() reads back whether that is so
 * and FAILS if it is not — it does not lock on the caller's behalf, because
 * the lock is process-wide and the probe owns no process-wide state, and it
 * does not warn and continue, because a hot path that can fault produces
 * latency spikes with no relation to the thing being measured.
 *
 * What open() does do, per measured context: pre-faults this context's stack,
 * touches and retains its own buffers, opens counter descriptors for this
 * context, measures which time source is cheapest on this platform, and
 * calibrates its own per-event inflation.
 *
 * Fails atomically. Returns NULL with the reason in *err, and leaves *err
 * clear on success — there is no path that returns a probe and a message. */
mcib_probe_t *mcib_probe_open(const mcib_probe_config_t *cfg, mcib_error_t *err);

/* Hot path. Records ONE event.
 *
 * No allocation, no locking, no file I/O, no coordination with any other
 * context. Ordered with barriers so the compiler and the processor cannot
 * move surrounding work across the point where the timestamps are taken.
 *
 * The instrument's own cost falls partly inside any interval built from two
 * events and is not cancelled by symmetry — an interval contains the tail of
 * the opening mark and the preamble of the closing one, and no ordering makes
 * those equal while the instrument does any work at all. It is constant per
 * event, so a differential between two conditions is unaffected; only the
 * absolute value is biased. That bias is measured at open and written into
 * the record, so analysis subtracts a number rather than assuming one.
 *
 * NOT reentrant and not thread-safe: one probe instance serves exactly one
 * execution context. Nested entry from a signal handler or a second thread
 * corrupts the sequence counter and the write index. Builds with assertions
 * enabled check the calling context; measurement builds do not, because the
 * check is not free and would land inside the calibrated inflation.
 *
 * Making the indices atomic would make them safe and the timing meaningless:
 * a contended atomic in the hot path adds precisely the unbounded,
 * load-dependent cost this instrument exists to avoid. */
void mcib_probe_mark(mcib_probe_t *p, uint32_t point_id, uint64_t token);

/* Same, and additionally requests a kernel timestamp under the BOUNDARY
 * policy. Use at the endpoints of an interval. */
void mcib_probe_mark_boundary(mcib_probe_t *p, uint32_t point_id, uint64_t token);

/* Convenience for the single-context case ONLY. Strictly two marks; the model
 * does not require pairing and nothing downstream assumes it. */
void mcib_probe_enter(mcib_probe_t *p, uint32_t point_id, uint64_t token);
void mcib_probe_exit (mcib_probe_t *p, uint32_t point_id, uint64_t token);

/* ------------------------------------------------------- run boundaries */

/* Bracket the measurement loop — called after warm-up, and again when the
 * loop ends and before any teardown.
 *
 * Two things need the boundary and cannot infer it. The record states when
 * the measured run began and ended in both clock domains, and the probe is
 * the only component present at both instants. And the page-fault check is
 * only meaningful over the measured section: opened to closed would include
 * thread teardown, whose faults say nothing about the hot path.
 *
 * Calling neither is allowed and is recorded as such: the record then says
 * the bounds were inferred from the first and last event, and the fault
 * window says it spans open to close. */
void mcib_probe_run_begin(mcib_probe_t *p);
void mcib_probe_run_end  (mcib_probe_t *p);

/* Post-run. Publishes the buffer, writes the event stream and this context's
 * metadata, and releases resources. A buffer must not be read before its
 * writing context has called this. Returns 0 on success, -1 with *err set.
 *
 * `path` is required and must not already exist: the record is written once
 * and never rewritten, so an existing file is an error rather than something
 * to truncate. A run whose record cannot be written has produced nothing. */
int mcib_probe_close(mcib_probe_t *p, const char *path, mcib_error_t *err);

/* ------------------------------------------------------------ inspection */

uint64_t     mcib_probe_count    (const mcib_probe_t *p);
bool         mcib_probe_overflowed(const mcib_probe_t *p);

/* Page faults taken by this context across the measurement window. Any
 * increase means the pre-fault was incomplete and the run is suspect: a fault
 * inside the measured section is a latency spike with no relation to the
 * thing being measured. Memory was verifiably locked at open, so an
 * incomplete pre-fault is the one remaining cause.
 *
 * Returns true when the run is suspect. `valid` reports whether the counts
 * could be read at all; where it is false the counts are zero and say
 * nothing, and the run is reported suspect on the grounds that the check
 * could not be performed. */
bool         mcib_probe_faulted  (const mcib_probe_t *p,
                                  uint64_t *minor, uint64_t *major,
                                  bool *valid);
const mcib_event_t *mcib_probe_events(const mcib_probe_t *p, uint64_t *n);

/* Counter names in the order they appear in mcib_event_t.counters, for the
 * set this probe opened. Returns the number of counters. */
uint32_t     mcib_probe_counter_names(const mcib_probe_t *p,
                                      const char *names[MCIB_MAX_COUNTERS]);

/* ------------------------------------------------------------ instrument */

/* What the probe measured about itself at open. Recorded in every record; the
 * accessor exists so a victim can report it without parsing its own output.
 *
 * `null_interval_*` is the instrument's per-event inflation: the interval
 * between two back-to-back marks with nothing at all between them. A build
 * that still carries an assertion in the hot path, or that fell back to an
 * expensive clock, shows an inflation far above the expected tens of
 * nanoseconds — which is the point of recording it. */
typedef struct {
    const char *time_source;          /* the source selection chose        */
    uint64_t    time_source_cost_ns;  /* its measured per-read cost        */
    uint64_t    frequency_hz;         /* read from the platform            */
    uint64_t    null_interval_p50_ns;
    uint64_t    null_interval_p99_ns;
    uint32_t    calibration_events;
} mcib_instrument_t;

void mcib_probe_instrument(const mcib_probe_t *p, mcib_instrument_t *out);

/* Clock-domain descriptor for this context.
 *
 * Note what is absent: an origin identity. Two contexts can read the same
 * counter at the same frequency and still not share a time base — under a
 * hypervisor each guest sees the counter through its own offset, and that
 * offset is not readable from inside the guest. An origin is therefore
 * established by measuring a common reference from both sides, which this
 * context cannot do alone. It reports only what it can know. */
typedef struct {
    uint16_t    domain_id;
    const char *counter_identity;   /* the selected time source */
    uint64_t    frequency_hz;       /* read from the platform, not assumed */
    const char *execution_context;  /* as the caller declared it */
    const char *kernel_clock;       /* e.g. "CLOCK_MONOTONIC", NULL if none */
} mcib_clock_domain_t;

void mcib_probe_domain(const mcib_probe_t *p, mcib_clock_domain_t *out);

#ifdef __cplusplus
}
#endif
#endif /* MCIB_PROBE_H */
