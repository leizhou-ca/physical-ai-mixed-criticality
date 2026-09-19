/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Victim — intertask messaging latency.
 *
 * POSIX pipe round trip between two threads pinned to one isolated core.
 * One-way latency = round trip / 2, as the reference dataset defines it.
 *
 * This is a victim, not part of the harness core: the harness contribution
 * here is the probe calls and nothing else. A victim is replaceable by
 * construction — nothing in the library depends on it.
 *
 * By default the instrumentation is single-context — both marks are made by
 * the sender thread — so every interval is joinable exactly and every counter
 * segment is qualified on a backend that follows the thread.
 *
 * The echo server can optionally be instrumented too, for characterisation:
 * two tools measuring this metric can place their counter reads in different
 * threads, and where they are placed changes what the measured window
 * contains. The server's probe is a SEPARATE instance with its own buffer,
 * its own counter segment and its own record. Nothing is shared and no delta
 * is claimed across the two contexts; that separation is what makes a second
 * instrumented context safe to add at all. The sender's instrumentation is
 * unchanged by its presence.
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

enum { PT_SEND = 1, PT_RECV = 2 };

static int   opt_cpu      = 3;
static long  opt_iters    = 100000;
static int   opt_prio     = 95;
static long  opt_warmup   = 1000;
static int   opt_counters = 1;
static int   opt_kts      = MCIB_KTS_EVERY_EVENT;
static int   opt_relaxed  = 0;
static int   opt_srv_probe    = 0;   /* instrument the echo server too      */
static int   opt_srv_counters = 0;   /* ... and give its probe counters     */
static const char *opt_out = "imlat_events.csv";
static const char *opt_exec_context = "host";
static const char *opt_counter_set  = NULL;  /* NULL => platform default    */

static int to_server[2], to_client[2];

/* Written by the server thread, read by main after the join. Synchronised:
 * a plain flag plus a plain buffer lets a reader see the flag before the
 * message. */
static victim_status_t srv_status;

/* The server's record sits beside the sender's, with its context name before
 * the extension: a record carries one context's events, so two instrumented
 * contexts produce two files. Merging them is an analysis step, not the
 * probe's — it never sees another context's buffer. */
static void server_record_path(char *out, size_t n, const char *base)
{
    const char *dot = strrchr(base, '.');
    const char *slash = strrchr(base, '/');
    if (dot && (!slash || dot > slash))
        snprintf(out, n, "%.*s.server%s", (int)(dot - base), base, dot);
    else
        snprintf(out, n, "%s.server", base);
}

/* Returns 0 on success, -1 on failure. Under -R a scheduling failure is
 * reported and tolerated so the victim can be exercised on a development
 * host with no real-time privilege. That is safe to allow only because the
 * probe reads the context's ACTUAL policy, priority and CPU at open and
 * writes them into the record: a run taken without real-time scheduling says
 * so in its own header and cannot be mistaken for a measurement. */
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

/* Echo server: reads a byte, writes it back.
 *
 * Uninstrumented by default — the interval under measurement is the sender's
 * round trip. With -S it opens its own probe and brackets its read-and-write
 * with two marks, which is where a tool that instruments the delivery side
 * rather than the requesting side puts them. Its probe is opened from this
 * thread, after this thread's own affinity and priority are set, because
 * per-thread counters follow the calling thread. */
static void *server(void *arg)
{
    (void)arg;
    if (pin_and_prioritise(opt_cpu, opt_prio) != 0) return NULL;

    mcib_probe_t *sp = NULL;
    if (opt_srv_probe) {
        mcib_error_t serr = {0};
        mcib_probe_config_t scfg = {
            .metric            = "imlat",
            .context_name      = "server",
            .execution_context = opt_exec_context,
            .warmup_events     = (uint32_t)(opt_warmup * 2),
            .capacity          = (uint32_t)(opt_iters * 2 + 16),
            .counters          = opt_srv_counters != 0,
            .counter_set       = opt_counter_set,
            .kts_policy        = (mcib_kts_policy_t)opt_kts,
            .domain_id         = 0,
        };
        sp = mcib_probe_open(&scfg, &serr);
        if (!sp) {
            victim_fail(&srv_status, serr.msg);
            return NULL;
        }
        victim_print_instrument(sp, "server");
    }

    unsigned char b;
    long seen = 0;
    bool begun = false;
    for (;;) {
        if (sp) mcib_probe_mark_boundary(sp, PT_SEND, (uint64_t)seen + 1);
        ssize_t r = read(to_server[0], &b, 1);
        if (r != 1) break;
        if (write(to_client[1], &b, 1) != 1) break;
        if (sp) mcib_probe_mark_boundary(sp, PT_RECV, (uint64_t)seen + 1);

        seen++;
        /* The server brackets its own measured section from its own count,
         * without coordinating with the sender — the hot path must never do
         * that. The tests are >= and latched rather than ==: an equality test
         * silently never fires if the sender stops early, leaving the run
         * unbracketed and the fault window spanning teardown. A run that ends
         * early is then reported as short, which is a visible failure rather
         * than a quietly wrong window. */
        if (sp && !begun && seen >= opt_warmup) {
            mcib_probe_run_begin(sp);
            begun = true;
        }
    }
    if (sp && begun) mcib_probe_run_end(sp);

    if (sp) {
        mcib_error_t serr = {0};
        char path[512];
        server_record_path(path, sizeof path, opt_out);
        /* A half-failed run still writes what it has: the record is the
         * evidence of how it failed. */
        if (victim_short_run(mcib_probe_count(sp),
                             (uint64_t)opt_iters * 2, "server"))
            victim_fail(&srv_status, "server context recorded a short run");
        if (mcib_probe_close(sp, path, &serr) != 0)
            victim_fail(&srv_status, serr.msg);
    }
    return NULL;
}

static void usage(const char *me)
{
    fprintf(stderr,
      "usage: %s [-c cpu] [-n iters] [-P prio] [-w warmup] [-o out.csv]\n"
      "          [-K no counters in the sender] [-s counter-set]\n"
      "          [-S instrument the echo server] [-C counters in the server]\n"
      "          [-b boundary kernel timestamps] [-e execution-context]\n"
      "          [-R tolerate no real-time scheduling]\n"
      "\n"
      "The output path must not already exist: a record is written once.\n"
      "With -S the server writes a second record beside it, named for its\n"
      "context; the two are separate records of separate contexts.\n", me);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "c:n:P:w:o:e:s:KbRSC")) != -1) {
        switch (c) {
        case 'c': opt_cpu      = atoi(optarg); break;
        case 'n': opt_iters    = atol(optarg); break;
        case 'P': opt_prio     = atoi(optarg); break;
        case 'w': opt_warmup   = atol(optarg); break;
        case 'o': opt_out      = optarg;       break;
        case 'e': opt_exec_context = optarg;   break;
        case 's': opt_counter_set = optarg;    break;
        case 'K': opt_counters = 0;            break;
        case 'b': opt_kts      = MCIB_KTS_BOUNDARY; break;
        case 'R': opt_relaxed  = 1;            break;
        case 'S': opt_srv_probe    = 1;        break;
        case 'C': opt_srv_counters = 1;        break;
        default:  usage(argv[0]); return 2;
        }
    }

    if (opt_srv_counters && !opt_srv_probe) {
        fprintf(stderr, "victim: -C needs -S: counters in the server require "
                        "a probe in the server\n");
        return 2;
    }
    if (opt_iters <= 0 || opt_warmup < 0) {
        fprintf(stderr, "victim: iterations must be positive and warmup "
                        "non-negative\n");
        return 2;
    }
    /* The event buffer is sized in 32-bit slots, so the request has to fit
     * one before it is cast. Two events per iteration, plus warm-up. */
    long need = (opt_iters + opt_warmup) * 2 + 16;
    if (need > 0x7fffffffL) {
        fprintf(stderr, "victim: %ld iterations needs %ld event slots, more "
                        "than this build addresses\n", opt_iters, need);
        return 2;
    }

    /* Lock memory HERE, before the probe is opened. The lock is process-wide,
     * so it belongs to whoever owns the process; the probe verifies it and
     * refuses to open without it. The failure is reported and not acted on
     * here so that there is exactly one gate for this precondition, in the
     * probe, rather than two that could disagree. */
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        fprintf(stderr, "victim: mlockall failed: %s\n", strerror(errno));

    victim_status_init(&srv_status);

    if (pipe(to_server) != 0 || pipe(to_client) != 0) {
        perror("pipe"); return 1;
    }

    pthread_t srv;
    if (pthread_create(&srv, NULL, server, NULL) != 0) {
        perror("pthread_create"); return 1;
    }

    /* Order matters: affinity and priority BEFORE probe_open, because
     * per-thread counters follow the calling thread. */
    if (pin_and_prioritise(opt_cpu, opt_prio) != 0) return 1;

    mcib_error_t err = {0};
    mcib_probe_config_t cfg = {
        .metric            = "imlat",
        .context_name      = "sender",
        .execution_context = opt_exec_context,
        .warmup_events     = (uint32_t)(opt_warmup * 2), /* two marks per iter */
        .capacity          = (uint32_t)(opt_iters * 2 + 16),
        .counters          = opt_counters != 0,
        .counter_set       = opt_counter_set,
        .kts_policy        = (mcib_kts_policy_t)opt_kts,
        .domain_id         = 0,
    };

    mcib_probe_t *p = mcib_probe_open(&cfg, &err);
    if (!p) { fprintf(stderr, "%s\n", err.msg); return 1; }

    victim_print_instrument(p, "sender");

    unsigned char b = 0x5a;

    /* Warm-up first, then the measured loop between explicit boundaries. The
     * boundaries are what give the record its run extent in both clock
     * domains, and what keeps the page-fault window on the measured section
     * instead of spanning thread teardown. */
    for (long i = 0; i < opt_warmup; i++) {
        uint64_t token = (uint64_t)i + 1;
        mcib_probe_mark_boundary(p, PT_SEND, token);
        if (write(to_server[1], &b, 1) != 1) { perror("write"); break; }
        if (read(to_client[0], &b, 1) != 1)  { perror("read");  break; }
        mcib_probe_mark_boundary(p, PT_RECV, token);
    }

    mcib_probe_run_begin(p);
    for (long i = 0; i < opt_iters; i++) {
        uint64_t token = (uint64_t)(opt_warmup + i) + 1;
        mcib_probe_mark_boundary(p, PT_SEND, token);
        if (write(to_server[1], &b, 1) != 1) { perror("write"); break; }
        if (read(to_client[0], &b, 1) != 1)  { perror("read");  break; }
        mcib_probe_mark_boundary(p, PT_RECV, token);
    }
    mcib_probe_run_end(p);

    close(to_server[1]);
    pthread_join(srv, NULL);

    const char *smsg = NULL;
    bool srv_bad = victim_failed(&srv_status, &smsg);

    uint64_t n    = mcib_probe_count(p);
    int      over = mcib_probe_overflowed(p);

    uint64_t fmin = 0, fmaj = 0;
    bool     fok  = false;
    bool     suspect = mcib_probe_faulted(p, &fmin, &fmaj, &fok);

    bool short_run = victim_short_run(n, (uint64_t)opt_iters * 2, "sender");

    /* A half-failed run still writes what it has, then exits non-zero. */
    bool write_failed = (mcib_probe_close(p, opt_out, &err) != 0);
    if (write_failed) fprintf(stderr, "%s\n", err.msg);

    printf("events=%llu overflowed=%d faults=%llu/%llu verdict=%s out=%s\n",
           (unsigned long long)n, over,
           (unsigned long long)fmin, (unsigned long long)fmaj,
           suspect ? "suspect" : "clean", opt_out);

    if (srv_bad)
        fprintf(stderr, "victim: server context failed: %s\n", smsg);
    if (over)
        fprintf(stderr, "victim: event buffer overflowed — record is "
                        "incomplete and must not be used\n");
    if (write_failed || srv_bad || over || short_run) return 1;
    if (suspect) {
        /* A fault inside the measured section is a latency spike unrelated to
         * the thing being measured. Memory was verifiably locked at open, so
         * the remaining cause is a pre-fault that did not reach deep enough.
         * The run is not reported as a success. */
        fprintf(stderr,
                "victim: run is SUSPECT — %s. The record carries the same "
                "verdict; do not treat these samples as a clean measurement\n",
                fok ? "pages faulted inside the measured section"
                    : "the fault count could not be read, so the pre-fault "
                      "contract could not be checked");
        return 1;
    }
    return 0;
}
