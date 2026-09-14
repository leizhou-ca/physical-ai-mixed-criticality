# Mixed-Criticality on Shared Silicon: A Primer for Physical AI

*Background note for the MCIB project. Licensed CC BY 4.0.*

---

## The problem

A robot that runs learned perception and safety-critical motion control on one
SoC is running two workloads with incompatible characteristics:

| | Inference domain | Control domain |
|---|---|---|
| Cadence | ~1–10 Hz, best-effort | sub-millisecond, hard deadline |
| Failure mode | degraded output | loss of control authority |
| Resource behaviour | bursty, memory-hungry | small, predictable, latency-sensitive |

Consolidating both onto one piece of silicon is an economic decision that
almost every physical AI platform has now made. The consequence is that the two
workloads contend for shared resources — last-level cache, memory bandwidth,
interconnect, interrupt delivery — and that contention surfaces as jitter in
the control loop.

The failure is not that the model is inaccurate. It is that the platform
underneath both workloads has no contract about interference.

---

## What earlier measurement shows

This section summarises earlier work by the same author, not MCIB results.
MCIB has produced no measurements yet and shares no code with the work below.
That work is what inspired this project ([DOI
10.5281/zenodo.20967588](https://doi.org/10.5281/zenodo.20967588)) evaluated
AGL-RT with Linux PREEMPT_RT 6.12 on Arm Cortex-A72 (BCM2711, Raspberry Pi 4B),
across five POSIX RTOS primitives, four stressor profiles, and 500,000
iterations per configuration.

**The steady state is not the problem. The tail is.** Medians sit where a
real-time engineer would want them — task switching around 2.5 µs, semaphore
preemption around 1.5 µs, thread-mode IPC around 5.4 µs — and they barely move
across stressor profiles. P99.9 across all five metrics stays under 57 µs.

Maxima tell a different story. Task-switch latency reaches 140–168 µs against a
2.5 µs median. Process-mode inter-task messaging reaches 595 µs, three orders of
magnitude above its median. Mutex acquisition under balanced stress shows a
max/min ratio above 1,100×.

Two conclusions follow, and both matter more than the numbers.

**P99.9 is not a WCET bound.** A percentile that looks comfortable can sit two
orders of magnitude below the observed maximum. Any timing argument resting on
P99.9 alone is resting on sand.

**Isolation policy is not optional, and it is not partial.** The published work
found that the full policy set — CPU isolation, tickless operation, RCU
offloading, RT throttling disabled, and interrupt affinity — must be applied
together. Omitting any one still produces spikes two to three orders of
magnitude above median. Isolation without tickless configuration, for instance,
still admits timer-tick interrupts on the supposedly isolated core.

With the full policy enforced, tail behaviour is bounded.

---

## What is not yet established

The published work measured *that* the tails occur and *that* isolation policy
bounds them. It does not establish the microarchitectural mechanism behind each
outlier.

The mutex result under memory stress is consistent with cache-line eviction by
the aggressor, and cross-core acquisition cost is consistent with a coherency
round-trip over the interconnect. Both are hypotheses supported by the shape of
the data, not attributed findings. Correlating these outliers with hardware
performance counters is named in that paper as future work. That open question
is what MCIB was inspired by — though MCIB addresses a different problem,
inference-plus-control workloads on shared silicon, and needs its own harness
to do it.

This distinction is the whole point of the project. Knowing a control loop
occasionally takes 168 µs tells you that you have a problem. Knowing *why*
tells you whether configuration fixes it, whether hardware partitioning fixes
it, or whether nothing will.

---

## Two isolation architectures, often conflated

Discussions of mixed-criticality separation tend to blur two distinct
inter-domain transports. They are not interchangeable.

**RPMsg over remoteproc** is the pragmatic near-term choice for a bare-metal or
RTOS domain on a separate core. Well-trodden, widely supported in vendor BSPs,
less abstraction.

**VirtIO** is the direction aligned with SOAFEE and hypervisor-based
consolidation. It buys standardisation and portability at the cost of an
abstraction layer whose overhead must be measured against the control domain's
budget, not assumed away.

Both are legitimate; the deciding input should be measured inter-domain
overhead against the actual deadline.

Two Arm mechanisms sit underneath either choice. **SMMU** provides spatial
isolation, so the inference domain cannot write into control-domain memory.
**MPAM** or **CACHE COLORING** provides temporal partitioning of cache and bandwidth, so the
inference domain cannot starve the control domain's working set. MPAM is
available on Armv8.4 automotive-class parts and is identified in the published
work as a candidate enforcement layer for the residual cache pathway that CPU
isolation alone does not close.

Spatial isolation without temporal partitioning is a common and incomplete
configuration. Memory protection does not prevent a cache eviction.

---

## Measurement discipline, the hard-won part

Three constraints from building this framework, offered because they cost real
time to discover and generalise to any RT benchmarking work on Linux.

**Keep conversion out of the hot path.** Storing only the raw counter delta in
the measurement loop and deferring all arithmetic to a post-processing pass is
not an optimisation, it is a correctness requirement. Inline tick-to-nanosecond
conversion costs tens of nanoseconds on Cortex-A72 — a material fraction of a
2.5 µs interval — and produces visibly bimodal distributions.

**Watch the clock domain boundary.** Kernel GPIO character-device events carry
`CLOCK_MONOTONIC` timestamps. Pairing them with a user-space `T0` from
`CLOCK_MONOTONIC_RAW` introduces the NTP slew offset and yields latencies that
are either absurd or negative. Both endpoints of an interval spanning a kernel
event must use the same domain.

**Beware synchronous event delivery.** Driving a GPIO edge via ioctl fires the
edge synchronously inside the call. A single thread polling afterwards always
finds the event already queued and measures ioctl tail latency rather than
interrupt latency. A two-thread design with priority ordering is required for
the waiter to be genuinely blocked when the edge arrives.

---

## Why this is becoming a compliance question

For anyone shipping into the EU, two regulations turn the above from an
engineering preference into a dated obligation.

**Cyber Resilience Act — Regulation (EU) 2024/2847.** In force since
10 December 2024. Article 14 reporting obligations have applied since
**11 September 2026** — that date has passed. They cover products with digital
elements *already on the EU market*, regardless of when they were placed there;
there is no legacy exemption. On becoming aware of an actively exploited
vulnerability, a manufacturer owes an early warning within 24 hours, full
notification within 72 hours, and a final report within 14 days of a corrective
measure. The remaining obligations — essential requirements, conformity
assessment, CE marking — apply from **11 December 2027**.

**Machinery Regulation — Regulation (EU) 2023/1230.** Applies from **20 January
2027**, replacing Directive 2006/42/EC, and covering robots, cobots, and AMRs.
It is the legal mandate; harmonised standards such as IEC 61508, ISO 13849, and
ISO 10218 are routes to demonstrating conformity with it, not mandates in their
own right.

For software freedom-from-interference arguments specifically, **IEC 61508
Part 3** is the relevant standard. It is frequently confused with ISO 13849,
which addresses machinery safety functions at system level rather than the
software non-interference argument.

Sector-specific regimes take precedence within their domains: ISO/SAE 21434 and
UNECE R155/R156 for road vehicles, IEC 62443 for industrial automation. The CRA
is horizontal framework legislation, not a replacement for these.

---

## What this project does about it

MCIB measures interference between inference and control workloads on shared
silicon, attributes each outlier to a mechanism, and returns a verdict:
**tuning**, **partitionable**, or **ceiling**.

That output is characterisation data. It is not qualified safety evidence — the
toolchain is unqualified, and nothing here should be presented as certification
evidence. Characterisation data can be an input to a safety argument that a
qualified party assembles. The distinction is deliberate.

See the [project README](../README.md) for current status, including what has
been measured and what has not.

---

*Regulatory dates reflect the position as of September 2026 and are provided for
orientation, not as legal advice. Verify against the current official text
before relying on them.*

*Copyright 2026 Linaro Ltd. SPDX-License-Identifier: CC-BY-4.0*
