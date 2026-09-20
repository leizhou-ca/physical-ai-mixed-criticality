/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2026 Lei Zhou, Linaro
 *
 * Userspace counter-read capability probe.
 *
 * Arm parts are known to report this capability inconsistently, so this asks
 * the kernel per event rather than trusting a single flag: it opens events
 * under several attribute combinations, maps each one's page, and prints what
 * the page actually says — whether a userspace read is offered, which counter
 * index it names, and how wide that counter is.
 *
 * It measures nothing and writes no record. It answers one question: on this
 * image, for these events, is a register read available at all.
 *
 * On the reference platform the answer is no: the capability is reported
 * absent for every combination tried, and a counter read costs a syscall.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sched.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <linux/perf_event.h>

#define EV_L1D_CACHE_REFILL 0x03
#define EV_L2D_CACHE_REFILL 0x17
#define EV_CPU_CYCLES       0x11

static long peo(struct perf_event_attr *a, pid_t pid, int cpu, int gfd,
                unsigned long fl)
{ return syscall(__NR_perf_event_open, a, pid, cpu, gfd, fl); }

struct probe_case {
    const char *name;
    uint32_t    type;
    uint64_t    config;
    int         exclude_hv;
    int         exclude_kernel;
    int         grouped;      /* open as a member of a leader */
    int         percpu;       /* pid=-1, cpu=3 instead of pid=0, cpu=-1 */
};

static void report(const struct probe_case *c)
{
    struct perf_event_attr a;
    int leader = -1;

    if (c->grouped) {
        memset(&a, 0, sizeof a);
        a.type = PERF_TYPE_RAW; a.size = sizeof a;
        a.config = EV_CPU_CYCLES; a.disabled = 1;
        a.exclude_hv = c->exclude_hv; a.exclude_kernel = c->exclude_kernel;
        a.read_format = PERF_FORMAT_GROUP;
        leader = (int)(c->percpu ? peo(&a, -1, 3, -1, 0) : peo(&a, 0, -1, -1, 0));
        if (leader < 0) {
            printf("%-34s leader open failed: %s\n", c->name, strerror(errno));
            return;
        }
    }

    memset(&a, 0, sizeof a);
    a.type = c->type; a.size = sizeof a; a.config = c->config;
    a.disabled = (leader < 0);
    a.exclude_hv = c->exclude_hv;
    a.exclude_kernel = c->exclude_kernel;
    if (c->grouped) a.read_format = PERF_FORMAT_GROUP;

    int fd = (int)(c->percpu ? peo(&a, -1, 3, leader, 0) : peo(&a, 0, -1, leader, 0));
    if (fd < 0) {
        printf("%-34s open failed: %s\n", c->name, strerror(errno));
        if (leader >= 0) close(leader);
        return;
    }

    long psz = sysconf(_SC_PAGESIZE);
    struct perf_event_mmap_page *pc =
        mmap(NULL, (size_t)psz, PROT_READ, MAP_SHARED, fd, 0);
    if (pc == MAP_FAILED) {
        printf("%-34s mmap failed: %s\n", c->name, strerror(errno));
        close(fd); if (leader >= 0) close(leader);
        return;
    }

    printf("%-34s cap_user_rdpmc=%u index=%-3u pmc_width=%-3u "
           "cap_user_time=%u offset=%lld\n",
           c->name, pc->cap_user_rdpmc, pc->index, pc->pmc_width,
           pc->cap_user_time, (long long)pc->offset);

    munmap(pc, (size_t)psz);
    close(fd);
    if (leader >= 0) close(leader);
}

int main(void)
{
    FILE *f = fopen("/proc/sys/kernel/perf_user_access", "re");
    int ua = -1;
    if (f) { if (fscanf(f, "%d", &ua) != 1) ua = -1; fclose(f); }
    printf("perf_user_access = %d\n", ua);
    printf("(a userspace read is offered only when cap_user_rdpmc is 1 AND "
           "index is non-zero)\n\n");

    const struct probe_case cases[] = {
      { "cycles, as the default set opens", PERF_TYPE_RAW, EV_CPU_CYCLES, 1, 0, 1, 0 },
      { "cycles, ungrouped, exclude_hv=1",  PERF_TYPE_RAW, EV_CPU_CYCLES, 1, 0, 0, 0 },
      { "cycles, ungrouped, exclude_hv=0",  PERF_TYPE_RAW, EV_CPU_CYCLES, 0, 0, 0, 0 },
      { "cycles, ungrouped, all excl. off", PERF_TYPE_RAW, EV_CPU_CYCLES, 0, 0, 0, 0 },
      { "l1d_refill, ungrouped, hv=0",      PERF_TYPE_RAW, EV_L1D_CACHE_REFILL, 0, 0, 0, 0 },
      { "l2d_refill, ungrouped, hv=0",      PERF_TYPE_RAW, EV_L2D_CACHE_REFILL, 0, 0, 0, 0 },
      { "HW cycles (PERF_TYPE_HARDWARE)",   PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, 0, 0, 0, 0 },
      { "sw context switches",              PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CONTEXT_SWITCHES, 0, 0, 0, 0 },
      { "sw cpu migrations",                PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CPU_MIGRATIONS, 0, 0, 0, 0 },
      { "cycles, PER-CPU (pid=-1 cpu=3)",   PERF_TYPE_RAW, EV_CPU_CYCLES, 1, 0, 0, 1 },
    };

    cpu_set_t s; CPU_ZERO(&s); CPU_SET(3, &s);
    if (sched_setaffinity(0, sizeof s, &s) != 0)
        printf("(warning: could not pin to CPU3: %s)\n", strerror(errno));

    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++)
        report(&cases[i]);
    return 0;
}
