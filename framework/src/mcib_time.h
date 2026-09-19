/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Time source — selected by measurement, not by #if.
 *
 * Which free-running source is cheapest is a platform fact, not an
 * architectural one. On Arm an EL0 read of the virtual counter executes
 * without a trap only while the kernel leaves EL0 access enabled; where a
 * counter erratum workaround is active the kernel deliberately disables it,
 * turns off the vDSO fast path, and emulates the register read in an
 * exception handler. The frequency register shares the same control bit and
 * traps with it. On such a part the register read costs microseconds and the
 * monotonic clock degrades to a syscall for the same reason: neither source
 * is unconditionally cheap, and the gap between a trapped and an untrapped
 * read is three orders of magnitude.
 *
 * Choosing at compile time therefore gets one platform right and the other
 * wrong, silently, in a direction that inflates every interval. This picks by
 * timing each available source at open, and refuses to run at all if even the
 * cheapest is too expensive to measure with.
 */
#ifndef MCIB_TIME_H
#define MCIB_TIME_H

#include <stdint.h>
#include "mcib_probe.h"

typedef struct {
    const char *name;             /* "arch-counter", "clock-monotonic-raw" */
    uint64_t  (*read)(void);      /* free-running reading, source units    */
    uint64_t  (*frequency)(void); /* read from the platform, never assumed */
} mcib_time_source_t;

/* The cost above which a source is unusable for measurement.
 *
 * Reasoning for the value, so it can be argued with rather than guessed at:
 * an untrapped architectural counter read is a register move and costs tens
 * of nanoseconds; the monotonic clock through the vDSO measured 55.6 ns on
 * the Cortex-A72 reference board, roughly four times the register read; a
 * trapped read is an exception entry and costs microseconds. The ceiling has
 * to sit above the honest vDSO figure with room for a slower part, and far
 * below the trapped case. 200 ns is about 3.5x the measured vDSO cost and
 * about 1/5 of the cheapest plausible trap, so no reasonable fast path is
 * rejected and no trapped path is accepted.
 *
 * It is a ceiling on the INSTRUMENT, not on the workload: at 200 ns a source
 * would already consume 1.3% of the 15 us interval this harness was built to
 * measure, twice per event. Anything above that is not a degraded mode worth
 * continuing in. */
#define MCIB_TIME_SOURCE_MAX_COST_NS  200u

/* Burst shape for the selection measurement.
 *
 * Each burst reads the candidate MCIB_TIME_BURST_READS times between two
 * readings of a common reference clock, and the per-read cost is the
 * difference divided by that count. A common reference is used rather than
 * timing a candidate with itself because a candidate's own frequency may not
 * be readable until it has been shown to work — on the trapped-counter part
 * above, reading the frequency register is the very thing that faults.
 *
 * The burst is long enough that the two reference readings contribute under
 * 1% of the per-read figure even if the reference itself has degraded to a
 * syscall, and the median over the bursts discards a burst that was
 * preempted. */
#define MCIB_TIME_BURST_READS   128u
#define MCIB_TIME_BURSTS         33u   /* odd, so the median is a sample */

typedef struct {
    const mcib_time_source_t *src;
    uint64_t frequency_hz;      /* of the chosen source, read at selection  */
    uint64_t cost_ns;           /* median per-read cost of the chosen one   */
    uint64_t cost_ns_worst;     /* worst burst of the chosen one            */
    unsigned n_candidates;
    char     detail[96];        /* "name=cost_ns ..." for every candidate   */
} mcib_time_choice_t;

/* Times every available source, chooses the cheapest, reads that source's
 * frequency from the platform, and fails if the cheapest exceeds the ceiling
 * or reports no frequency. Returns 0 on success, -1 with *err set. */
int mcib_time_select(mcib_time_choice_t *out, mcib_error_t *err);

#endif /* MCIB_TIME_H */
