/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Victim — task preemption latency.
 *
 * A low-priority trigger thread signals; a high-priority waiter thread wakes
 * and runs. Both pinned to one isolated core, the waiter above the trigger.
 * The interval runs from the signal to the waiter resuming.
 *
 * This is the token-paired cross-context case, and it is the rule the
 * middleware configuration will use. The interval spans two contexts, as the
 * task-switching case does, but unlike it there IS a correspondence to carry:
 * the Nth signal causes the Nth wake, and both threads know which iteration
 * they are in from their own loop counter. The token is that iteration
 * identity.
 *
 * The only synchronisation between the two threads is the semaphore pair that
 * IS the workload — a preemption benchmark with no signal has nothing to
 * measure — plus a barrier before warm-up so both are running before either
 * measures. Nothing was added to make the measurement work.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include "mcib_probe.h"
#include "victim_common.h"

/* The opening point is the signal, the closing point is the wake. The token
 * says which iteration; the record says which context.
 *
 * PT_WAIT_ENTER is not an end of the interval. It is where the WAITER's
 * counter segment opens: the last instruction before it blocks. The interval
 * still runs from the trigger's mark to the waiter's, and still pairs on the
 * token — what this third point provides is a counter delta that belongs to
 * ONE context and brackets the block, so it contains the wakeup path and
 * nothing else. Per-thread counters stop while the thread is blocked, so the
 * time spent waiting contributes no counts.
 *
 * It costs nothing inside the interval. The waiter is the higher-priority
 * thread on a core it shares with the trigger, so the trigger cannot run —
 * and cannot signal — until the waiter has blocked. This mark is therefore
 * always taken before the interval opens, never inside it. */
enum { PT_TRIGGER = 1, PT_RESUME = 2, PT_WAIT_ENTER = 3 };

static int   opt_cpu      = 3;
static long  opt_iters    = 100000;
static int   opt_prio     = 95;      /* the WAITER's priority                */
static int   opt_prio_gap = 1;       /* trigger runs this much lower         */
static long  opt_warmup   = 1000;
static int   opt_counters = 1;
static int   opt_relaxed  = 0;
static const char *opt_out = "prelat_events.csv";
static const char *opt_exec_context = "host";
static const char *opt_counter_set = "armv3-sched";

/* CHARACTERISATION ONLY — deliberately non-conformant, default off.
 *
 * It adds shared state between two measured
 * contexts in the hot path, which a victim must never do; it exists only to
 * put a number on what that costs, and a run using it is a characterisation
 * run and not a measurement.
 *
 *   0  off — the shipped arrangement
 *   1  release store in the trigger, inside the interval 
 *   2  as 1, plus the acquire load in the waiter, outside the interval
 */
static int opt_atomic = 0;
static atomic_uint_fast64_t shared_trigger_time;
static volatile uint64_t    atomic_sink;

static sem_t sem_trigger;   /* trigger -> waiter: wake up            */
static sem_t sem_reset;     /* waiter -> trigger: I am done, go again */

static pthread_barrier_t start_barrier;
static victim_status_t   pre_status;

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

/* `marks_per_iteration` is not cosmetic. The waiter records two events per
 * iteration and the trigger one, so the buffer it needs and the number of
 * warm-up events it must discard are twice the trigger's. Sizing both from
 * the iteration count alone would silently truncate the waiter's record. */
static mcib_probe_t *open_context(const char *ctx, long marks_per_iteration)
{
    mcib_error_t err = {0};
    mcib_probe_config_t cfg = {
        .metric            = "prelat",
        .context_name      = ctx,
        .execution_context = opt_exec_context,
        .warmup_events     = (uint32_t)(opt_warmup * marks_per_iteration),
        .capacity          = (uint32_t)(opt_iters * marks_per_iteration + 16),
        .counters          = opt_counters != 0,
        .counter_set       = opt_counter_set,
        .kts_policy        = MCIB_KTS_EVERY_EVENT,
        /* One domain: same process, same core, same counter register. Each
         * record states the identity and frequency it read, so the claim is
         * checkable rather than trusted. */
        .domain_id         = 0,
    };
    mcib_probe_t *p = mcib_probe_open(&cfg, &err);
    if (!p) {
        victim_fail(&pre_status, err.msg);
        return NULL;
    }
    victim_print_instrument(p, ctx);
    return p;
}

/* Closes a context: acts on the fault verdict, flags a short run, and writes
 * what it has either way. */
static void close_context(mcib_probe_t *p, const char *ctx,
                          long marks_per_iteration)
{
    uint64_t fmin = 0, fmaj = 0;
    bool fok = false;
    if (mcib_probe_faulted(p, &fmin, &fmaj, &fok)) {
        char m[MCIB_ERR_LEN];
        snprintf(m, sizeof m,
                 "context '%s' is SUSPECT — %s (minor %llu, major %llu)", ctx,
                 fok ? "pages faulted inside the measured section"
                     : "the fault count could not be read",
                 (unsigned long long)fmin, (unsigned long long)fmaj);
        victim_fail(&pre_status, m);
    }
    if (victim_short_run(mcib_probe_count(p),
                         (uint64_t)(opt_iters * marks_per_iteration), ctx))
        victim_fail(&pre_status, "a measured context recorded a short run");

    mcib_error_t err = {0};
    char path[512];
    context_record_path(path, sizeof path, opt_out, ctx);
    if (mcib_probe_close(p, path, &err) != 0)
        victim_fail(&pre_status, err.msg);
}

/* Low priority. Signals, then blocks until the waiter is done. */
static void *trigger(void *arg)
{
    (void)arg;
    if (pin_and_prioritise(opt_cpu, opt_prio - opt_prio_gap) != 0) {
        victim_fail(&pre_status, "could not pin or prioritise the trigger");
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }
    mcib_probe_t *p = open_context("trigger", 1);
    if (!p) { pthread_barrier_wait(&start_barrier); return NULL; }

    pthread_barrier_wait(&start_barrier);

    long i = 0;
    for (; i < opt_warmup; i++) {
        mcib_probe_mark_boundary(p, PT_TRIGGER, (uint64_t)i + 1);
        if (opt_atomic)
            atomic_store_explicit(&shared_trigger_time, (uint_fast64_t)i,
                                  memory_order_release);
        sem_post(&sem_trigger);
        sem_wait(&sem_reset);
    }
    mcib_probe_run_begin(p);
    for (; i < opt_warmup + opt_iters; i++) {
        /* The token is the iteration identity the workload already has: the
         * Nth signal causes the Nth wake. Nothing is written for the other
         * context to read, and nothing crosses but the signal itself. */
        mcib_probe_mark_boundary(p, PT_TRIGGER, (uint64_t)i + 1);
        /* Inside the interval, exactly where the predecessor put it. */
        if (opt_atomic)
            atomic_store_explicit(&shared_trigger_time, (uint_fast64_t)i,
                                  memory_order_release);
        sem_post(&sem_trigger);
        sem_wait(&sem_reset);
    }
    mcib_probe_run_end(p);

    close_context(p, "trigger", 1);
    return NULL;
}

/* High priority. Wakes on the signal and marks; the mark is the first thing
 * it does, so the interval is the signal-to-execution path and not the
 * workload after it. */
static void *waiter(void *arg)
{
    (void)arg;
    if (pin_and_prioritise(opt_cpu, opt_prio) != 0) {
        victim_fail(&pre_status, "could not pin or prioritise the waiter");
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }
    mcib_probe_t *p = open_context("waiter", 2);
    if (!p) { pthread_barrier_wait(&start_barrier); return NULL; }

    pthread_barrier_wait(&start_barrier);

    long i = 0;
    for (; i < opt_warmup; i++) {
        mcib_probe_mark(p, PT_WAIT_ENTER, (uint64_t)i + 1);
        sem_wait(&sem_trigger);
        mcib_probe_mark_boundary(p, PT_RESUME, (uint64_t)i + 1);
        if (opt_atomic > 1)
            atomic_sink = (uint64_t)atomic_load_explicit(&shared_trigger_time,
                                                         memory_order_acquire);
        sem_post(&sem_reset);
    }
    mcib_probe_run_begin(p);
    for (; i < opt_warmup + opt_iters; i++) {
        /* The counter segment opens here, in this context, and closes at the
         * resume mark below. Nothing is differenced across the two threads. */
        mcib_probe_mark(p, PT_WAIT_ENTER, (uint64_t)i + 1);
        sem_wait(&sem_trigger);
        mcib_probe_mark_boundary(p, PT_RESUME, (uint64_t)i + 1);
        /* Outside the interval, exactly where the predecessor put it. */
        if (opt_atomic > 1)
            atomic_sink = (uint64_t)atomic_load_explicit(&shared_trigger_time,
                                                         memory_order_acquire);
        sem_post(&sem_reset);
    }
    mcib_probe_run_end(p);

    close_context(p, "waiter", 2);
    return NULL;
}

static void usage(const char *me)
{
    fprintf(stderr,
      "usage: %s [-c cpu] [-n iters] [-P waiter-prio] [-g prio-gap]\n"
      "          [-w warmup] [-o out.csv] [-K no counters] [-s counter-set]\n"
      "          [-e execution-context] [-R tolerate no real-time scheduling]\n"
      "          [-A 0|1|2 CHARACTERISATION ONLY: reintroduce the shared\n"
      "                    atomic in the hot path; a run using it is not a\n"
      "                    measurement]\n"
      "\n"
      "Two contexts, two records, named for the context that wrote them.\n"
      "Neither path may already exist: a record is written once.\n", me);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "c:n:P:g:w:o:e:s:A:KR")) != -1) {
        switch (c) {
        case 'c': opt_cpu      = atoi(optarg); break;
        case 'n': opt_iters    = atol(optarg); break;
        case 'P': opt_prio     = atoi(optarg); break;
        case 'g': opt_prio_gap = atoi(optarg); break;
        case 'w': opt_warmup   = atol(optarg); break;
        case 'o': opt_out      = optarg;       break;
        case 'e': opt_exec_context = optarg;   break;
        case 's': opt_counter_set  = optarg;   break;
        case 'A': opt_atomic   = atoi(optarg); break;
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
    if (opt_prio_gap < 1 || opt_prio - opt_prio_gap < 1) {
        fprintf(stderr, "victim: the waiter must run above the trigger, and "
                        "both must be valid real-time priorities\n");
        return 2;
    }
    long need = opt_iters + opt_warmup + 16;
    if (need > 0x7fffffffL) {
        fprintf(stderr, "victim: %ld iterations needs %ld event slots, more "
                        "than this build addresses\n", opt_iters, need);
        return 2;
    }

    if (opt_atomic < 0 || opt_atomic > 2) {
        fprintf(stderr, "victim: -A takes 0, 1 or 2\n"); return 2;
    }
    if (opt_atomic)
        fprintf(stderr, "victim: CHARACTERISATION MODE -A %d — a shared "
                        "atomic is in the hot path. This run measures the "
                        "instrument, not the platform\n", opt_atomic);
    atomic_init(&shared_trigger_time, 0);

    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        fprintf(stderr, "victim: mlockall failed: %s\n", strerror(errno));

    victim_status_init(&pre_status);
    if (sem_init(&sem_trigger, 0, 0) != 0 || sem_init(&sem_reset, 0, 0) != 0) {
        perror("sem_init"); return 1;
    }
    if (pthread_barrier_init(&start_barrier, NULL, 2) != 0) {
        perror("pthread_barrier_init"); return 1;
    }

    pthread_t tt, tw;
    if (pthread_create(&tw, NULL, waiter, NULL) != 0 ||
        pthread_create(&tt, NULL, trigger, NULL) != 0) {
        perror("pthread_create"); return 1;
    }
    pthread_join(tt, NULL);
    /* The waiter is blocked on the semaphore once the trigger stops; release
     * it so it can close its own record rather than being left to a join that
     * never returns. */
    sem_post(&sem_trigger);
    pthread_join(tw, NULL);

    pthread_barrier_destroy(&start_barrier);
    sem_destroy(&sem_trigger);
    sem_destroy(&sem_reset);

    char pt[512], pw[512];
    context_record_path(pt, sizeof pt, opt_out, "trigger");
    context_record_path(pw, sizeof pw, opt_out, "waiter");

    const char *msg = NULL;
    if (victim_failed(&pre_status, &msg)) {
        fprintf(stderr, "victim: %s\n", msg);
        return 1;
    }

    printf("contexts=2 iterations=%ld records=%s,%s\n", opt_iters, pt, pw);
    printf("intervals are reconstructed off-target by the token-pair rule; "
           "no interval or statistic is computed here\n");
    return 0;
}
