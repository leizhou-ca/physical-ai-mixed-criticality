<!--
SPDX-License-Identifier: CC-BY-4.0
Copyright © 2026 Linaro Ltd.
https://creativecommons.org/licenses/by/4.0/
-->

# Why this project exists

## The problem

Physical AI puts an inference workload and a real-time control loop on the
same silicon. A robot perceives, infers, plans, and acts — and increasingly
all four stages share one SoC, because power and thermal budgets left no
other option.

That sharing is not free. The inference workload and the control loop
contend for last-level cache, memory bandwidth, the interconnect, and the
hypervisor's scheduler. Under contention, the control loop's worst-case
latency moves. Sometimes by a little. Sometimes past its deadline.

**The hard part is not measuring that latency. It is explaining it.**

An engineer who sees a deadline miss has three possible situations in front
of them, and no way to tell which:

1. A configuration problem — wrong core affinity, wrong scheduling policy,
   a missing isolation setting. Fixable in an afternoon.
2. A resource-partitioning problem — two workloads genuinely contending for
   a shared resource, fixable with a partitioning mechanism the platform
   already has.
3. A structural limit — the platform cannot do both things at once, and no
   amount of tuning will change that.

Today, telling these apart is guesswork. Engineers iterate parameters,
re-measure, and iterate again — distinguishing a tuning problem from a
fundamental ceiling by exhaustion rather than by evidence. Weeks disappear
into this.

## Why existing tools do not close it

The ecosystem has good tools, and MCIB composes with all of them rather
than replacing any:

- **Latency measurement** tools tell you *what* the latency was.
- **Application tracers** tell you *where* in the graph the time went.
- **Performance benchmarks** tell you *how fast* a workload runs.
- **Isolation mechanisms** let you *act* once you know what to do.

What none of them tells you is *why the worst case happened* and *which
countermeasure applies*. That is the gap MCIB fills, and it sits between
the "where" layer and the "act" layer.

## Why it is getting harder

Two trends run against the engineer.

**The interesting behaviour moved off the CPU.** Accelerators are DMA
masters. They generate memory traffic that bypasses CPU-side isolation
entirely, so a control loop can be perturbed by a device the operating
system's scheduler never sees.

**Observability is shrinking.** Each accelerator generation is more closed
than the last. Queue state lives inside a vendor runtime; DMA behaviour
lives inside a driver; occupancy is visible only through vendor tooling, if
at all.

This is why MCIB's foundational technique is **black-box differential
attribution** — measure the victim, toggle the aggressor, attribute the
delta, corroborate with hardware counters where the silicon exposes them.
It is the only method in the stack that survives regardless of how open the
vendor chooses to be.

## What "why" looks like concretely

A published example, from outside this project. Measuring an object
detector on an embedded GPU platform, Jang et al. found a reported cycle
time of 163 ms against a measured end-to-end delay of about 1,070 ms —
roughly six times. The excess was not in the network. It came from a frame
queue, from idle gaps between unbalanced pipeline stages, and from memory
bandwidth contention between CPU threads and the integrated GPU. Their
fixes were to the system architecture, not the neural network, and cut
average delay by 76% and the 99th-percentile delay by 67%, with no loss of
detection accuracy.

> W. Jang, H. Jeong, K. Kang, N. Dutt, J.-C. Kim. *R-TOD: Real-Time Object
> Detector with Minimized End-to-End Delay for Autonomous Driving.* RTSS
> 2020. DOI [10.1109/RTSS49844.2020.00027](https://doi.org/10.1109/RTSS49844.2020.00027)

Nobody could have tuned their way to that result without first knowing
where the time actually went. That is the class of problem MCIB exists to
make routine.

## A first measurement

The method is small enough to demonstrate on a four-core Arm development board
running PREEMPT_RT Linux — one core isolated for a latency-sensitive task, the
others loaded with synthetic CPU, memory and I/O work. This is a method
demonstration on hardware with no accelerator and no partitioning mechanism,
not a result about the silicon the project is aimed at.

The first attempt said the aggressors were making the task *faster*. They were
not. The per-run CPU clock, recorded alongside each measurement, had varied
between 800 MHz and 1.3 GHz depending on how loaded the board was, and the
clock tracked the latency exactly. Verdict: `tune` — pin the governor. With
the clock fixed, the four medians agree to within 0.3 µs.

Only then does the real interference appear, and it is not where the first
numbers pointed. Every aggressor leaves the typical case untouched and raises
the worst case by roughly the same 6 µs. Hardware counters show no channel
elevated; the task is on-CPU and executing throughout; no traced kernel event
coincides with the slow samples. Nothing available at configuration level on
that kernel build moves it. Verdict: `ceiling`.

Two verdicts, one afternoon, from a metric that would otherwise have produced
a table of numbers and no explanation. That is the whole argument for this
project in miniature: the measurement was never the hard part.

The same exercise re-measured metrics from the earlier work that inspired this
project, and several of its figures did not reproduce once the clock was pinned
and the interval rule was stated. Those qualifications are in the
[primer](mixed-criticality-primer.md), because a project arguing that numbers
need attribution cannot exempt its own antecedents.

## Objectives

**1. Make the verdict the deliverable, not the number.**
Every MCIB result ends in an attributed verdict and a countermeasure:
`tune`, `partition`, or `ceiling`. A number without a verdict is a
measurement, not an MCIB result.

**2. Make results comparable across platforms.**
Two datasets from different silicon, produced by different people, are
either comparable or visibly not comparable. That is what the
specification is for: defined metrics, a declared measurement boundary, a
channel taxonomy, and provenance requirements that make a result
reconstructible by someone who was not there.

**3. Make the framework portable, so the verdict travels.**
The specification is platform-neutral and ISA-neutral. Ports contribute
platform support and datasets; the core stays small enough for one or two
maintainers indefinitely. A benchmark that only runs where its authors run
it is not evidence, it is an anecdote.

**4. Render the same evidence for assessors.**
Interference characterisation data, provenance-stamped and reproducible,
in a form a functional-safety assessor can consume. This is a derivative
output of the same attribution work, not a separate product.

## What this project is not

Not a latency measurement tool. Not an application tracer. Not a
performance benchmark. Not an isolation mechanism. MCIB composes with
each and replaces none.

## Where it stands

Early, and honest about it. The specification is drafted and public. The
harness exists — probe, four victims, interval reconstruction, counter and
trace evidence, and an orchestrator — and verdict assignment is being built.

The measurement above was produced with the method on predecessor
instrumentation, while the harness was under construction; reproducing it is
one of the harness's acceptance criteria, and it does. **No conforming dataset
has been published**, and this file will say so until one has — see the status
table in the README.

What is not yet shown: a `partition` verdict, which needs a platform with a
resource-partitioning mechanism, and any result with a real inference
workload as the aggressor rather than synthetic load. That second one
matters, because the premise of this project is that real accelerators
interfere differently from the synthetic stressors the field has been using.
Testing that premise is the next piece of work, not a settled claim.

The most useful thing anyone outside this project can do right now is tell
us where the channel taxonomy misses something they have actually hit, or
port the framework to silicon we do not have and send back a conforming
dataset.

---

*© 2026 Linaro Ltd. CC BY 4.0.*
