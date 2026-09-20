/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Victim — interrupt handling latency.
 *
 * A low-priority trigger thread drives a GPIO line; the edge travels down a
 * wire into a second line; a high-priority waiter thread is blocked in poll()
 * on that line and marks as soon as it returns.
 *
 * WHAT THE INTERVAL CONTAINS, stated because it is easy to misread. The
 * opening mark is taken immediately BEFORE the ioctl that drives the pin, so
 * the measured quantity spans: the ioctl into the kernel, the pin driving,
 * the edge firing, interrupt delivery through the interrupt controller, the
 * kernel's wake path, and poll() returning. It is NOT a bare interrupt
 * delivery figure and must not be read as one. The record says so in its
 * metric description, so a reader who never saw this file cannot make that
 * mistake.
 *
 * WHY BOTH ENDS ARE ON THE SAME CLOCK, and which one. Neither end of this
 * interval is stamped by the kernel: both are marks taken by this process in
 * userspace, the second one as the first instruction after poll() returns.
 * The kernel's own event timestamp exists — the GPIO character device
 * delivers one — and this victim does not read it, exactly as the tool it is
 * being compared against does not. Both ends are therefore free to use the
 * probe's free-running reading, which on this platform is the architectural
 * counter, and both do. The kernel time base is recorded alongside on every
 * event regardless, so alignment against a kernel trace is not given up.
 *
 * Mixing the two bases across the ends of one interval would import the
 * accumulated offset between them, which is unbounded and has been measured
 * on this board in the tens of milliseconds. Nothing here does that: one
 * interval, one base, both ends.
 *
 * NO SHARED STATE IN THE HOT PATH. The pipe the waiter uses to announce that
 * it is about to block is the workload's own protocol — a trigger with no way
 * to know the waiter is armed would measure a race, not a latency. The token
 * is the iteration identity both sides derive from that lockstep. Nothing is
 * added to the measured section to make the measurement work: no shared
 * timestamp, no shared counter array, and no result passed back for the
 * trigger to subtract. Each context writes its own record and pairing happens
 * off the target.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/gpio.h>

#include "mcib_probe.h"
#include "victim_common.h"

/* PT_WAIT_ENTER is not an end of the interval. It is where the WAITER's
 * counter segment opens: the last instruction before it blocks in poll().
 * The interval still runs from the trigger's mark to the waiter's, and still
 * pairs on the token — what this third point provides is a counter delta
 * belonging to ONE context and bracketing the block, so it contains the
 * interrupt-delivery and wake path and nothing else. Per-thread counters stop
 * while the thread is blocked, so the wait itself contributes no counts.
 *
 * It is taken AFTER the ready-pipe write and before poll(), and that order is
 * what keeps it outside the interval. The trigger is the lower-priority
 * thread on the same core: the write unblocks it, but it cannot be scheduled
 * until the waiter blocks. So the mark is always taken before the trigger can
 * drive the edge, never inside the measured section. */
enum { PT_TRIGGER = 1, PT_RESUME = 2, PT_WAIT_ENTER = 3 };

/* Wiring, fixed: GPIO17 (header pin 11) out, GPIO27 (header pin 13) in,
 * looped by a wire, through the character device. */
#define GPIO_CHIP      "/dev/gpiochip0"
#define GPIO_LINE_OUT  17
#define GPIO_LINE_IN   27
#define POLL_TIMEOUT_MS 1000

static int   opt_cpu      = 3;
static long  opt_iters    = 100000;
static int   opt_prio     = 95;      /* the WAITER's priority */
static int   opt_prio_gap = 1;
static long  opt_warmup   = 1000;
static int   opt_counters = 1;
static int   opt_relaxed  = 0;
static const char *opt_out = "inlat_events.csv";
static const char *opt_exec_context = "host";
static const char *opt_counter_set = "armv3-util";

static int out_fd = -1, in_fd = -1;
static int ready_pipe[2] = { -1, -1 };   /* waiter -> trigger: "I am arming" */

static pthread_barrier_t start_barrier;
static victim_status_t   in_status;

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

/* ------------------------------------------------------------------ gpio */

static int gpio_open_lines(void)
{
    int chip = open(GPIO_CHIP, O_RDWR | O_CLOEXEC);
    if (chip < 0) {
        fprintf(stderr, "victim: open %s: %s\n", GPIO_CHIP, strerror(errno));
        return -1;
    }

    struct gpio_v2_line_request req;
    memset(&req, 0, sizeof req);
    req.offsets[0] = GPIO_LINE_OUT;
    req.num_lines  = 1;
    req.config.flags = GPIO_V2_LINE_FLAG_OUTPUT;
    snprintf(req.consumer, sizeof req.consumer, "mcib-inlat-out");
    if (ioctl(chip, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
        fprintf(stderr, "victim: request GPIO%d as output: %s\n",
                GPIO_LINE_OUT, strerror(errno));
        close(chip); return -1;
    }
    out_fd = req.fd;

    memset(&req, 0, sizeof req);
    req.offsets[0] = GPIO_LINE_IN;
    req.num_lines  = 1;
    req.config.flags = GPIO_V2_LINE_FLAG_INPUT |
                       GPIO_V2_LINE_FLAG_EDGE_RISING;
    snprintf(req.consumer, sizeof req.consumer, "mcib-inlat-in");
    if (ioctl(chip, GPIO_V2_GET_LINE_IOCTL, &req) < 0) {
        fprintf(stderr, "victim: request GPIO%d as rising-edge input: %s\n",
                GPIO_LINE_IN, strerror(errno));
        close(chip); return -1;
    }
    in_fd = req.fd;
    close(chip);
    return 0;
}

static int gpio_set(int value)
{
    struct gpio_v2_line_values v;
    memset(&v, 0, sizeof v);
    v.mask = 1;
    v.bits = value ? 1 : 0;
    return ioctl(out_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, &v);
}

static void gpio_drain(void)
{
    struct gpio_v2_line_event ev;
    struct pollfd p = { .fd = in_fd, .events = POLLIN };
    while (poll(&p, 1, 0) > 0 && (p.revents & POLLIN))
        if (read(in_fd, &ev, sizeof ev) != (ssize_t)sizeof ev) break;
}

/* Continuity check, before anything is measured.
 *
 * A missing or broken loopback wire produces a run of poll() timeouts, which
 * looks like a platform result and is not one. Finding out at the start costs
 * one edge; finding out at the end costs the run. */
static int gpio_check_loopback(void)
{
    gpio_set(0);
    gpio_drain();
    if (gpio_set(1) < 0) {
        fprintf(stderr, "victim: cannot drive GPIO%d: %s\n",
                GPIO_LINE_OUT, strerror(errno));
        return -1;
    }
    struct pollfd p = { .fd = in_fd, .events = POLLIN };
    int r = poll(&p, 1, 200);
    gpio_set(0);
    if (r <= 0) {
        fprintf(stderr,
                "victim: no edge seen on GPIO%d within 200 ms of driving "
                "GPIO%d. The loopback wire between header pin 11 and pin 13 "
                "is missing or broken — this is a wiring fault, not a "
                "measurement\n", GPIO_LINE_IN, GPIO_LINE_OUT);
        return -1;
    }
    gpio_drain();
    return 0;
}

/* ---------------------------------------------------------------- threads */

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
        .metric            = "inlat",
        .context_name      = ctx,
        .execution_context = opt_exec_context,
        .warmup_events     = (uint32_t)(opt_warmup * marks_per_iteration),
        .capacity          = (uint32_t)(opt_iters * marks_per_iteration + 16),
        .counters          = opt_counters != 0,
        .counter_set       = opt_counter_set,
        /* Both readings on every event: the interval is built from the
         * free-running one, the kernel one is there for trace alignment. */
        .kts_policy        = MCIB_KTS_EVERY_EVENT,
        .domain_id         = 0,
    };
    mcib_probe_t *p = mcib_probe_open(&cfg, &err);
    if (!p) { victim_fail(&in_status, err.msg); return NULL; }
    victim_print_instrument(p, ctx);
    return p;
}

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
        victim_fail(&in_status, m);
    }
    if (victim_short_run(mcib_probe_count(p),
                         (uint64_t)(opt_iters * marks_per_iteration), ctx))
        victim_fail(&in_status, "a measured context recorded a short run");

    mcib_error_t err = {0};
    char path[512];
    context_record_path(path, sizeof path, opt_out, ctx);
    if (mcib_probe_close(p, path, &err) != 0)
        victim_fail(&in_status, err.msg);
}

/* High priority. Announces that it is arming, blocks on the line, and marks
 * as the first thing it does when poll() returns.
 *
 * The announcement is made before entering poll(), which on one core with
 * this thread at the higher priority is safe: after the write it runs
 * straight into poll() and blocks there, and only then does the trigger get
 * the core. That ordering is what the priority gap is for. */
static void *waiter(void *arg)
{
    (void)arg;
    if (pin_and_prioritise(opt_cpu, opt_prio) != 0) {
        victim_fail(&in_status, "could not pin or prioritise the waiter");
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }
    mcib_probe_t *p = open_context("waiter", 2);
    if (!p) { pthread_barrier_wait(&start_barrier); return NULL; }
    pthread_barrier_wait(&start_barrier);

    unsigned char one = 1;
    long total = opt_warmup + opt_iters;
    for (long i = 0; i < total; i++) {
        struct pollfd pfd = { .fd = in_fd, .events = POLLIN };
        if (write(ready_pipe[1], &one, 1) != 1) break;

        /* The counter segment opens here, in this context, and closes at the
         * resume mark below. Nothing is differenced across the two threads. */
        mcib_probe_mark(p, PT_WAIT_ENTER, (uint64_t)i + 1);
        int r = poll(&pfd, 1, POLL_TIMEOUT_MS);
        mcib_probe_mark_boundary(p, PT_RESUME, (uint64_t)i + 1);

        if (r <= 0) {
            victim_fail(&in_status,
                        r == 0 ? "poll() timed out waiting for the edge — "
                                 "check the GPIO loopback wire"
                               : "poll() failed");
            break;
        }
        struct gpio_v2_line_event ev;
        /* Drained to re-arm the edge. Its timestamp is deliberately not the
         * interval's end: this victim measures the same quantity the tool it
         * is compared against measures, and that one ends at poll() return. */
        if (read(in_fd, &ev, sizeof ev) != (ssize_t)sizeof ev) break;

        if (i == opt_warmup - 1) mcib_probe_run_begin(p);
        if (i == total - 1)      mcib_probe_run_end(p);
    }

    close_context(p, "waiter", 2);
    return NULL;
}

/* Low priority. Waits for the waiter to arm, marks, then drives the edge. */
static void *trigger(void *arg)
{
    (void)arg;
    if (pin_and_prioritise(opt_cpu, opt_prio - opt_prio_gap) != 0) {
        victim_fail(&in_status, "could not pin or prioritise the trigger");
        pthread_barrier_wait(&start_barrier);
        return NULL;
    }
    mcib_probe_t *p = open_context("trigger", 1);
    if (!p) { pthread_barrier_wait(&start_barrier); return NULL; }
    pthread_barrier_wait(&start_barrier);

    unsigned char b;
    long total = opt_warmup + opt_iters;
    for (long i = 0; i < total; i++) {
        if (read(ready_pipe[0], &b, 1) != 1) break;

        /* Reset from the previous iteration. The line is armed for a rising
         * edge only, so lowering it fires nothing. */
        gpio_set(0);

        /* The opening mark is the last thing before the ioctl: the drive is
         * inside the interval, as the compared tool has it. */
        mcib_probe_mark_boundary(p, PT_TRIGGER, (uint64_t)i + 1);
        if (gpio_set(1) < 0) {
            victim_fail(&in_status, "driving the GPIO line failed");
            break;
        }

        if (i == opt_warmup - 1) mcib_probe_run_begin(p);
        if (i == total - 1)      mcib_probe_run_end(p);
    }

    close_context(p, "trigger", 1);
    return NULL;
}

static void usage(const char *me)
{
    fprintf(stderr,
      "usage: %s [-c cpu] [-n iters] [-P waiter-prio] [-g prio-gap]\n"
      "          [-w warmup] [-o out.csv] [-K no counters] [-s counter-set]\n"
      "          [-e execution-context] [-R tolerate no real-time scheduling]\n"
      "\n"
      "Requires a loopback wire from header pin 11 (GPIO%d) to pin 13\n"
      "(GPIO%d); continuity is checked before the run. Two contexts, two\n"
      "records, named for the context that wrote them.\n",
      me, GPIO_LINE_OUT, GPIO_LINE_IN);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "c:n:P:g:w:o:e:s:KR")) != -1) {
        switch (c) {
        case 'c': opt_cpu      = atoi(optarg); break;
        case 'n': opt_iters    = atol(optarg); break;
        case 'P': opt_prio     = atoi(optarg); break;
        case 'g': opt_prio_gap = atoi(optarg); break;
        case 'w': opt_warmup   = atol(optarg); break;
        case 'o': opt_out      = optarg;       break;
        case 'e': opt_exec_context = optarg;   break;
        case 's': opt_counter_set  = optarg;   break;
        case 'K': opt_counters = 0;            break;
        case 'R': opt_relaxed  = 1;            break;
        default:  usage(argv[0]); return 2;
        }
    }
    if (opt_iters <= 0 || opt_warmup <= 0) {
        fprintf(stderr, "victim: iterations and warmup must be positive\n");
        return 2;
    }
    if (opt_prio_gap < 1 || opt_prio - opt_prio_gap < 1) {
        fprintf(stderr, "victim: the waiter must run above the trigger, and "
                        "both must be valid real-time priorities\n");
        return 2;
    }
    if (opt_iters + opt_warmup + 16 > 0x7fffffffL) {
        fprintf(stderr, "victim: too many iterations for this build\n");
        return 2;
    }

    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        fprintf(stderr, "victim: mlockall failed: %s\n", strerror(errno));

    victim_status_init(&in_status);
    if (gpio_open_lines() != 0) return 1;
    if (gpio_check_loopback() != 0) return 1;
    printf("gpio: loopback GPIO%d -> GPIO%d verified before the run\n",
           GPIO_LINE_OUT, GPIO_LINE_IN);
    fflush(stdout);

    if (pipe(ready_pipe) != 0) { perror("pipe"); return 1; }
    if (pthread_barrier_init(&start_barrier, NULL, 2) != 0) {
        perror("pthread_barrier_init"); return 1;
    }

    pthread_t tw, tt;
    if (pthread_create(&tw, NULL, waiter, NULL) != 0 ||
        pthread_create(&tt, NULL, trigger, NULL) != 0) {
        perror("pthread_create"); return 1;
    }
    pthread_join(tt, NULL);
    /* Release the waiter if it is still armed, so it closes its own record
     * rather than being left in a join that never returns. */
    gpio_set(0); gpio_set(1);
    pthread_join(tw, NULL);
    gpio_set(0);

    pthread_barrier_destroy(&start_barrier);
    close(in_fd); close(out_fd);
    close(ready_pipe[0]); close(ready_pipe[1]);

    const char *msg = NULL;
    if (victim_failed(&in_status, &msg)) {
        fprintf(stderr, "victim: %s\n", msg);
        return 1;
    }

    char pt[512], pw[512];
    context_record_path(pt, sizeof pt, opt_out, "trigger");
    context_record_path(pw, sizeof pw, opt_out, "waiter");
    printf("contexts=2 iterations=%ld records=%s,%s\n", opt_iters, pt, pw);
    printf("interval spans the drive ioctl, the edge, interrupt delivery and "
           "the poll wakeup; it is not a bare interrupt-delivery figure\n");
    printf("intervals are reconstructed off-target by the token-pair rule; "
           "no interval or statistic is computed here\n");
    return 0;
}
