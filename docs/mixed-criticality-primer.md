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

This section summarises earlier work by the same author
([DOI 10.5281/zenodo.20967588](https://doi.org/10.5281/zenodo.20967588)), not
MCIB results. MCIB shares no code with it. That work evaluated AGL-RT with
Linux PREEMPT_RT 6.12 on Arm Cortex-A72 (BCM2711, Raspberry Pi 4B), across five
POSIX RTOS primitives and four stressor profiles.

**The steady state is not the problem. The tail is.** Medians sit where a
real-time engineer would want them — a few microseconds for task switching and
semaphore preemption, single-digit microseconds for thread-mode IPC — and they
barely move across stressor profiles.

**Maxima tell a different story.** Every metric's observed maximum sits far
above its median, and the gap grows under load. That gap, not the median, is
what a deadline argument has to survive.

**P99.9 is not a WCET bound.** A percentile that looks comfortable can sit well
below the observed maximum. Any timing argument resting on P99.9 alone is
resting on sand. This conclusion is robust: it does not depend on the size of
any particular maximum, only on maxima exceeding the percentile — which they do
in every dataset either project has produced.

### A note on re-measurement, and why it is here

Building MCIB's harness meant measuring the same metrics on the same board
again, with two things the earlier work did not have: a CPU clock pinned and
recorded per run, and an interval-construction rule stated explicitly and
applied uniformly.

**Several of the earlier figures do not reproduce under those conditions.**

- **Medians agree closely.** Task switching, preemption and messaging medians
  land within a microsecond of the earlier values.
- **Maxima come out substantially lower** — by roughly an order of magnitude on
  task switching. Two mechanisms are identified. The earlier reconstruction
  rule, when matching a thread's yield to the resume that follows it, could
  match across a whole ping-pong cycle rather than to the immediately following
  event; applied to a modern capture, that rule and a strict one differ by a
  factor of three on identical data. And the CPU clock was not pinned, which
  moves a median by up to 30% in a direction that makes a loaded system look
  faster than an idle one.
- **The isolation-policy claim needs qualification.** The earlier work
  concluded that the full policy set — CPU isolation, tickless operation, RCU
  offloading, RT throttling disabled, interrupt affinity — must be applied
  together. On the image used, the kernel **rejects** the tickless and
  RCU-offload parameters: they appear in the command line, the boot log reports
  the feature unsupported, and both are passed to user space as unknown. The
  policy as described was therefore not the policy in force. The conclusion may
  well hold on a kernel built with those options; on that board it was untested.

None of this changes the direction of the earlier findings, and the qualitative
conclusions — tails matter, percentiles are not bounds, isolation helps — hold
in both datasets. What changes is how much confidence a specific number
carries.

**This is stated here rather than left for someone else to find**, because a
project whose thesis is that measurements need attribution cannot exempt its
own antecedents from it. A number without its conditions is not reproducible,
and the conditions were not recorded.

---

## What is not yet established

The earlier work measured *that* the tails occur and *that* isolation policy
bounds them. It does not establish the microarchitectural mechanism behind each
outlier.

Its mutex result under memory stress is consistent with cache-line eviction by
the aggressor, and cross-core acquisition cost is consistent with a coherency
round-trip over the interconnect. Both are hypotheses supported by the shape of
the data, not attributed findings. Correlating those outliers with hardware
performance counters is named in that paper as future work.

That open question is what MCIB was inspired by — though MCIB addresses a
different problem, inference-plus-control workloads on shared silicon, and
needs its own harness to do it.

This distinction is the whole point of the project. Knowing a control loop
occasionally takes far longer than its median tells you that you have a
problem. Knowing *why* tells you whether configuration fixes it, whether
hardware partitioning fixes it, or whether nothing will.

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
**MPAM**, or hypervisor cache colouring, provides temporal partitioning of cache
and bandwidth, so the inference domain cannot starve the control domain's
working set. MPAM is an optional extension on Armv8.4 and later parts and is
absent from much of the silicon in the field today, including both boards this
project has measured.

Spatial isolation without temporal partitioning is a common and incomplete
configuration. Memory protection does not prevent a cache eviction.

---

## Measurement discipline, the hard-won part

Constraints from building this framework, offered because they cost real time
to discover and generalise to any RT benchmarking work on Linux.

**Keep conversion out of the hot path.** Storing only the raw counter delta in
the measurement loop and deferring all arithmetic to a post-processing pass is
not an optimisation, it is a correctness requirement. Inline tick-to-nanosecond
conversion costs tens of nanoseconds on Cortex-A72 — a material fraction of a
few-microsecond interval — and produces visibly bimodal distributions.

**Both ends of an interval must be in one clock domain.** A user-space
`CLOCK_MONOTONIC_RAW` timestamp paired with anything derived from
`CLOCK_MONOTONIC` carries the NTP slew between them, which is unbounded over a
long run and yields latencies that are absurd or negative. Pick one base per
interval, and record which was used.

**Beware synchronous event delivery.** Driving a GPIO edge via ioctl fires the
edge synchronously inside the call. A single thread polling afterwards always
finds the event already queued and measures ioctl tail latency rather than
interrupt latency. A two-thread design with priority ordering is required for
the waiter to be genuinely blocked when the edge arrives.

**Record the conditions with the measurement.** The CPU governor, the clock the
run actually ran at, the isolation settings that were *active* rather than
requested, and what else was running. Every figure this project has had to
qualify was qualified because a condition was not recorded, not because the
measurement was careless.

**State the rule that turns events into intervals.** Two defensible rules
applied to one capture differed by a factor of three, and nothing in either
output showed which had been used.

---

## Why this is becoming a compliance question

For anyone shipping into the EU, two regulations turn the above from an
engineering preference into a dated obligation.

**Cyber Resilience Act — Regulation (EU) 2024/2847.** In force since
10 December 2024. Article 14 reporting obligations have applied since
**11 September 2026**. They cover products with digital elements *already on
the EU market*, regardless of when they were placed there; there is no legacy
exemption. On becoming aware of an actively exploited vulnerability, a
manufacturer owes an early warning within 24 hours, full notification within
72 hours, and a final report within 14 days of a corrective measure. The
remaining obligations — essential requirements, conformity assessment, CE
marking — apply from **11 December 2027**.

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
`tune`, `partition`, or `ceiling`.

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
