<!--
SPDX-License-Identifier: CC-BY-4.0
Copyright (c) 2026 Lei Zhou, Linaro
-->

# framework

The measurement side of MCIB: a probe library, the victims that use it, and
the tools that turn what they record into a verdict.

The project's purpose, the gap it fills and what the verdicts mean are in the
[top-level README](../README.md). This file is about running it.

---

## What is here

```
include/          the probe's public interface — one header
src/              the probe, counter backends, platform event maps
victims/          workloads that measure something and record events
analysis/         events → intervals → evidence → verdict
orchestrator/     declares a run, checks the board, sequences it
characterisation/ tools that answer a question about a platform, once
```

---

## Build

```bash
cmake -S framework -B build
cmake --build build
```

Nothing but a C11 compiler and pthreads. The analysis and orchestrator side is
Python 3; the orchestrator needs PyYAML to read a configuration, and nothing
else.

Cross-compiling is the normal case — the boards this runs on often have no
compiler:

```bash
cmake -S framework -B build-arm64 \
      -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc \
      -DCMAKE_EXE_LINKER_FLAGS=-static
cmake --build build-arm64
```

A static build removes the question of whether the target's libc is new
enough. Copy the binaries across; nothing else needs to be installed there.

---

## The shortest path to a result

```bash
mcib init platform-survey > survey.yaml   # a working config, commented
mcib explain survey.yaml                  # what it will do, and how long
mcib check survey.yaml                    # is a measurement here valid?
mcib run survey.yaml                      # measure
```

`check` is the one to run first on a new board. It answers whether a
measurement would be valid **without measuring**, reports every precondition
it found unmet rather than stopping at the first, and tells you what to do
about each. Most of what makes a run invalid is board state, and most of that
is fixable in a line.

The smallest configuration that works is four lines:

```yaml
session: my-first-run
victim: { metrics: [imlat] }
output: ./results/
```

Everything else has a default, and the run records the configuration it
actually resolved to — copy that back as the next starting point.

---

## The pipeline

```
collect  ──►  derive  ──►  attribute  ──►  report
 events      intervals      evidence        verdict
```

Four stages, and each is a command in its own right. A user who never
decomposes the pipeline should not have to; a user porting the framework needs
every stage separately.

**Events are primary.** A victim records timestamped events and computes no
latency. Intervals are derived afterwards by a named rule, off the measured
board. That is not ceremony: an interval that spans two threads belongs to
neither of them, and computing it in the victim would mean the measured
contexts coordinating inside the section being measured.

It also means a rule can be corrected and re-applied to data already taken. On
one recorded stream, two defensible reconstruction rules differed by a factor
of three, and nothing in either result would have shown which had been used.

---

## What makes a number here worth trusting

**The instrument measures itself.** At startup the probe times each available
clock source, picks one, and calibrates its own per-event cost. Both figures go
into the record, so you subtract a measured overhead rather than an assumed
one. On some metrics that overhead exceeds the thing being measured — the
record says so rather than leaving you to discover it.

**Every record states its own conditions.** Clock governor and frequency,
temperature, isolation, scheduling policy, what else was running. A latency
without its conditions is not comparable with anything, including itself an
hour later: an unpinned CPU governor moved a median by 30% in this project's
own data, in the direction that made a loaded system look faster than an idle
one.

**Counter deltas say whether they can be trusted.** A backend declares what it
can promise — whether counting follows the context, whether it can detect
preemption — and a segment that the backend cannot vouch for is marked, not
quietly included.

**Ratios carry their denominators.** An enrichment computed from an absent
denominator is reported as no signal, never as infinity. That specific failure
produced a confident verdict, with a recommended countermeasure, on a run that
had no aggressor running at all.

**The rule is part of the measurement.** Every derived result names the rule
and version that produced it. Comparing two datasets starts by comparing their
rules.

---

## What the platform must provide

| Needed | Why |
|---|---|
| A real-time scheduling policy | the victim must not be preempted by ordinary work |
| An isolated CPU | so the measured context is not sharing with the scheduler's other customers |
| A pinnable clock | an unpinned governor changes the measurement, not just the workload |
| Performance counters | tier-1 attribution; without them latency is still measured |
| Kernel tracepoints | tier-2 attribution; without them a software channel cannot be named |

**What is missing is reported, not worked around.** A platform with no
tracepoint support gets latency and counter attribution, and is told that
software-channel attribution is unavailable there. A kernel option that cannot
be enabled at runtime is a platform requirement, stated as one.

---

## What verdicts a platform can actually produce

`tune` and `ceiling` need only the platform's own configuration surface.

**`partition` needs a partitioning mechanism to exist** — cache partitioning,
colouring, an IOMMU with the right controls. The reference platform for this
work has none of them, so no `partition` verdict has ever been issued there,
and none can be. That is a property of the board, not a gap in the tool, and
the tool says which mechanisms it looked for.

---

## Porting it

A new SoC needs a counter map: event codes are per-microarchitecture, and an
unverified part is refused at open rather than silently mismatched. A new OS
needs a counter backend, declaring what it can promise. A new metric is a
victim and a rule, and nothing above them changes.

There is no conformance self-test yet. A port is currently checked by
reproducing a dataset, which is a poor answer for anyone without one — it is
the next thing this framework needs.

---

## Status

Early, and honest about it. Four victims measure OS primitives; attribution
produces evidence and the verdict stage is being built; the orchestrator runs
single-domain sessions, with the configuration format already shaped for
sessions that span guests or processors.

Measurements exist and are reproducible. No conforming public dataset has been
published yet, and this file will say so until one has.
