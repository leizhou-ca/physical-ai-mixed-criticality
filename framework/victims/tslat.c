/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Victim — voluntary task-switching latency.
 *
 * Two threads of equal real-time priority, pinned to one isolated core, each
 * yielding in a loop. The quantity measured is the interval from one thread
 * issuing its yield to the other thread resuming.
 *
 * This is the cross-context case, and it is the reason the probe's primitive
 * is an event rather than a paired interval. The interval belongs to neither
 * thread: one ends it, the other begins it, and no token can be carried
 * across because nothing passes between them — a yield transfers a core, not
 * a message. A paired API could only express it by having the two threads
 * coordinate inside the measured section, and coordination there costs an
 * unbounded, load-dependent amount of time, which is exactly what a latency
 * instrument must not add.
 *
 * So there is NO shared state between the threads in the measured loop. Not a
 * token, not a counter, not a flag. Each thread owns a probe, writes only its
 * own buffer, and records two events per iteration: one before its yield and
 * one after it returns. The transitions are reconstructed afterwards, off the
 * target, from the two event streams merged on their timestamps. The only
 * synchronisation between the threads is a barrier before warm-up, so that
 * both are running before either measures; it is initialisation and executes
 * once.
 *
 * Both threads are in one clock domain — one process, one core, one counter
 * read through the same register at the same frequency — so they are given
 * the same domain id, and each record states the counter identity and
 * frequency it actually read so that a reader can check the claim rather than
 * take it on trust.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "mcib_probe.h"
#include "victim_common.h"

/* The two points a thread records. Which thread recorded them is the record's
 * context, so the pair plus the context is everything the reconstruction
 * needs — and both are already fields of an event. */
enum { PT_YIELD_OUT = 1, PT_RESUME_IN = 2 };

static int   opt_cpu      = 3;
static long  opt_iters    = 100000;
static int   opt_prio     = 95;
static long  opt_warmup   = 1000;
static int   opt_counters = 1;
static int   opt_relaxed  = 0;
static const char *opt_out = "tslat_events.csv";
static const char *opt_exec_context = "host";
static const char *opt_counter_set  = "armv3-sched";

static pthread_barrier_t start_barrier;

/* Written by either measured context, read by main after the join. */
static victim_status_t ts_status;

/* Both contexts are symmetric, so neither takes the bare path: each record is
 * named for the context that wrote it. A record carries one context's events;
 * merging the two is an analysis step and never the probe's, which cannot see
 * another context's buffer. */
static void context_record_path(char *out, size_t n, const char *base,
                                const char *ctx)
{
    const char *dot = strrchr(base, '.');
    const char *slash = strrchr(base, '/');
    if (dot && (!slash || dot > slash))
        snprintf(out, n, "%.*s.%s%s", (int)(dot - base), base, ctx, dot);
    else
        snprintf(out, n, "%s.%s", base, ctx);
}

static int pin_and_prioritise(int cpu, int prio)
{
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof set, &set) != 0) {
        fprintf(stderr, "victim: cannot pin to CPU%d: %s\n",
                cpu, strerror(errno));
        return -1;
    }
    struct sched_param sp = { .sched_priority = prio };
    if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0) {
        fprintf(stderr, "victim: cannot set SCHED_FIFO %d: %s%s\n",
                prio, strerror(errno),
                opt_relaxed ? " — continuing without real-time scheduling, "
                              "the record will show the policy actually used"
                            : "");
        return opt_relaxed ? 0 : -1;
    }
    return 0;
}

/* One measured context. Identical in both threads: the metric is symmetric,
 * and any asymmetry between them would land in the measurement. */
static void *yielder(void *arg)
{
    const char *ctx = (const char *)arg;

    if (pin_and_prioritise(opt_cpu, opt_prio) != 0) {
        victim_fail(&ts_status, "could not pin or prioritise a measured context");
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }

    mcib_error_t err = {0};
    mcib_probe_config_t cfg = {
        .metric            = "tslat",
        .context_name      = ctx,
        .execution_context = opt_exec_context,
        .warmup_events     = (uint32_t)(opt_warmup * 2),  /* two marks/iter */
        .capacity          = (uint32_t)(opt_iters * 2 + 16),
        .counters          = opt_counters != 0,
        .counter_set       = opt_counter_set,
        .kts_policy        = MCIB_KTS_EVERY_EVENT,
        /* One domain: same process, same core, same counter register. The
         * record states the identity and frequency so the claim is checkable. */
        .domain_id         = 0,
    };

    mcib_probe_t *p = mcib_probe_open(&cfg, &err);
    if (!p) {
        victim_fail(&ts_status, err.msg);
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }
    victim_print_instrument(p, ctx);

    /* Initialisation, not hot path: both threads must be runnable before
     * either measures, or the first yields have no counterpart to switch to.
     * Executes once, before warm-up. */
    pthread_barrier_wait(&start_barrier);

    for (long i = 0; i < opt_warmup; i++) {
        mcib_probe_mark_boundary(p, PT_YIELD_OUT, 0);
        sched_yield();
        mcib_probe_mark_boundary(p, PT_RESUME_IN, 0);
    }

    mcib_probe_run_begin(p);
    for (long i = 0; i < opt_iters; i++) {
        /* No token: nothing is carried across a yield. The pairing is
         * reconstructed after the run from the merged streams. */
        mcib_probe_mark_boundary(p, PT_YIELD_OUT, 0);
        sched_yield();
        mcib_probe_mark_boundary(p, PT_RESUME_IN, 0);
    }
    mcib_probe_run_end(p);

    /* The fault verdict is acted on here, in the context that owns it: a
     * suspect run is one whose samples contain page faults unrelated to the
     * thing being measured. Reported, not silently carried into the record's
     * verdict field alone. */
    uint64_t fmin = 0, fmaj = 0;
    bool fok = false;
    if (mcib_probe_faulted(p, &fmin, &fmaj, &fok)) {
        char m[MCIB_ERR_LEN];
        snprintf(m, sizeof m,
                 "context '%s' is SUSPECT — %s (minor %llu, major %llu)", ctx,
                 fok ? "pages faulted inside the measured section"
                     : "the fault count could not be read",
                 (unsigned long long)fmin, (unsigned long long)fmaj);
        victim_fail(&ts_status, m);
    }

    if (victim_short_run(mcib_probe_count(p), (uint64_t)opt_iters * 2, ctx))
        victim_fail(&ts_status, "a measured context recorded a short run");

    /* A half-failed run still writes what it has. */
    char path[512];
    context_record_path(path, sizeof path, opt_out, ctx);
    if (mcib_probe_close(p, path, &err) != 0)
        victim_fail(&ts_status, err.msg);
    return NULL;
}

static void usage(const char *me)
{
    fprintf(stderr,
      "usage: %s [-c cpu] [-n iters] [-P prio] [-w warmup] [-o out.csv]\n"
      "          [-K no counters] [-s counter-set] [-e execution-context]\n"
      "          [-R tolerate no real-time scheduling]\n"
      "\n"
      "Two contexts, two records, named for the context that wrote them.\n"
      "Neither path may already exist: a record is written once.\n", me);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "c:n:P:w:o:e:s:KR")) != -1) {
        switch (c) {
        case 'c': opt_cpu      = atoi(optarg); break;
        case 'n': opt_iters    = atol(optarg); break;
        case 'P': opt_prio     = atoi(optarg); break;
        case 'w': opt_warmup   = atol(optarg); break;
        case 'o': opt_out      = optarg;       break;
        case 'e': opt_exec_context = optarg;   break;
        case 's': opt_counter_set  = optarg;   break;
        case 'K': opt_counters = 0;            break;
        case 'R': opt_relaxed  = 1;            break;
        default:  usage(argv[0]); return 2;
        }
    }

    if (opt_iters <= 0 || opt_warmup < 0) {
        fprintf(stderr, "victim: iterations must be positive and warmup "
                        "non-negative\n");
        return 2;
    }
    long need = (opt_iters + opt_warmup) * 2 + 16;
    if (need > 0x7fffffffL) {
        fprintf(stderr, "victim: %ld iterations needs %ld event slots, more "
                        "than this build addresses\n", opt_iters, need);
        return 2;
    }

    /* Process-wide, so it belongs to whoever owns the process. Each probe
     * verifies it and refuses to open without it. */
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        fprintf(stderr, "victim: mlockall failed: %s\n", strerror(errno));

    victim_status_init(&ts_status);

    if (pthread_barrier_init(&start_barrier, NULL, 2) != 0) {
        perror("pthread_barrier_init"); return 1;
    }

    pthread_t ta, tb;
    if (pthread_create(&ta, NULL, yielder, (void *)"a") != 0 ||
        pthread_create(&tb, NULL, yielder, (void *)"b") != 0) {
        perror("pthread_create"); return 1;
    }
    pthread_join(ta, NULL);
    pthread_join(tb, NULL);
    pthread_barrier_destroy(&start_barrier);

    const char *msg = NULL;
    if (victim_failed(&ts_status, &msg)) {
        fprintf(stderr, "victim: %s\n", msg);
        return 1;
    }

    char pa[512], pb[512];
    context_record_path(pa, sizeof pa, opt_out, "a");
    context_record_path(pb, sizeof pb, opt_out, "b");
    printf("contexts=2 iterations=%ld records=%s,%s\n", opt_iters, pa, pb);
    printf("intervals are reconstructed off-target from the two streams; "
           "no interval is computed here\n");
    return 0;
}
