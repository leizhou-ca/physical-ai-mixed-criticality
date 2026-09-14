# MCIB — Mixed-Criticality Interference Benchmark

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20967588.svg)](https://doi.org/10.5281/zenodo.20967588)
[![License: Apache 2.0](https://img.shields.io/badge/code-Apache--2.0-blue.svg)](LICENSE)
[![License: CC BY 4.0](https://img.shields.io/badge/docs-CC%20BY%204.0-lightgrey.svg)](LICENSE-docs)

**An open benchmark for measuring — and attributing — interference between AI
inference and real-time control on shared silicon.**

When a robot runs perception and motor control on the same SoC, the control
loop gets slower. Existing tools tell you *where* the time went. MCIB is
concerned with *why the hardware made it slow*, and whether it is fixable.

---

## The gap this fills

`ros2_tracing`, CARET, and `Autoware_Perf` instrument the ROS 2 graph. They
tell you which callback ran long, which chain missed its budget, where latency
accumulated. That is the *where* layer, and it is well served.

None of them tell you whether that outlier was an LLC eviction, a scheduler
ceiling, or bus saturation — or which of those you can do something about.

MCIB is a layer underneath, not a competitor beside. Its loop is:

```
profile  →  attribute  →  countermeasure
```

and it terminates in a verdict for each measured outlier:

| Verdict | Meaning |
|---|---|
| **Tuning** | Fixable by configuration (affinity, isolation, scheduling policy) |
| **Partitionable** | Fixable by hardware partitioning (MPAM, cache coloring, SMMU) |
| **Ceiling** | A structural limit of the platform; no configuration will move it |

A benchmark that reports numbers without a verdict leaves the reader with
homework. MCIB treats the verdict as the deliverable.

---

## Status — read this before citing anything here

MCIB is a new project. It has produced no measurements yet. Everything in this
repository is either specification or plan, and each claim is labelled.

| Tier | What exists | Where |
|---|---|---|
| **Measured** | *(nothing yet)* | — |
| **Specified** | Interference-channel taxonomy, instrument-agnostic attribution protocol, results schema with mandatory provenance | `spec/` |
| **Planned** | Campaign #1 (Renesas R-Car V4H); Campaign #2 (ROS-native capture) | `campaigns/` |

If you are looking for results, there are none here today. What there is: a
schema you can target, a taxonomy you can argue with, and a plan you can tell
me is wrong. All three are more useful to challenge now than after data exists.

### What inspired this project

MCIB was inspired by earlier benchmark work by the same author on POSIX RTOS
primitives under Linux PREEMPT_RT ([DOI
10.5281/zenodo.20967588](https://doi.org/10.5281/zenodo.20967588), Embedded
World 2026). That work is **not** MCIB's data, and MCIB shares no code with
it. It measured POSIX primitives on Arm Cortex-A72 with no AI inference
workload present. Two things it surfaced led to this project:

1. Worst-case tails run two to three orders of magnitude above median, and
   P99.9 is not a safe WCET bound.
2. It characterised *that* the tails occur, not *why*. Attributing them to
   microarchitectural mechanisms was left as follow-on work.

MCIB starts from that second observation, for a different problem:
inference-plus-control workloads on shared silicon, rather than POSIX
primitives measured in isolation.

### On safety evidence

MCIB is a workload characterisation and diagnosis tool. Its output is
characterisation data, not qualified safety evidence: the toolchain is
unqualified, and no claim here should be read as certification evidence for
ISO 26262, IEC 61508, or any other standard. Characterisation data can be an
*input* to a safety argument that someone else qualifies. That distinction is
load-bearing and is enforced throughout the specification.

---

## What is in this repository

```
spec/         Interference taxonomy, attribution protocol, results schema
campaigns/    Per-platform measurement campaign plans
docs/         Background notes, problem framing, regulatory context
framework/    Benchmark harness (skeleton — see Status)
```

---

## Reference platform

Campaign #1 targets the **Sparrow Hawk SBC** (Renesas R-Car V4H) with the
community SDVoS BSP stack. The specification itself is platform-neutral and
ISA-neutral; the reference implementation is Arm-first because that is where
the mixed-criticality silicon is.

---

## Contributing

The most valuable contributions right now, in order:

1. **Conforming datasets from other platforms.** The schema in `spec/` exists
   so that measurements taken elsewhere are comparable. A dataset from silicon
   we do not have is worth more than a feature.
2. **Review of the attribution protocol.** If the taxonomy misses an
   interference channel you have hit in production, open an issue.
3. **ROS-native capture.** See Campaign #2.

A design constraint worth stating up front: MCIB **integrates with** the
existing ROS tracing ecosystem and does not fork or reimplement it. If a
contribution duplicates `ros2_tracing` or CARET, the right answer is to use
them instead.

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## At ROSCon 2026

I will be at ROSCon Global 2026 in Toronto, September 22–24. If you are
building on Arm silicon and fighting determinism under AI load, I would like to
compare notes — particularly if you have measurements that contradict anything
here. Open an issue or reach me via the contact on my
[profile](https://github.com/leizhou-ca).

---

## Licence

- **Code** (`framework/`, tooling, scripts): Apache License 2.0 — see [LICENSE](LICENSE)
- **Documents and datasets** (`spec/`, `campaigns/`, `docs/`): Creative Commons
  Attribution 4.0 International — see [LICENSE-docs](LICENSE-docs)

## Citing this work

The earlier work that inspired this project:

> Zhou, L. (2026). *Is Linux RT-PREEMPT Ready for Automotive Safety-Critical
> Workloads? A Systematic Benchmark Evaluation.* Embedded World Conference
> 2026, Nuremberg, Germany, March 10–12, 2026, Session 2.3 — RTOS
> Orchestration. Zenodo. https://doi.org/10.5281/zenodo.20967588

## Maintainer

Lei Zhou — Linaro Ltd. ([ORCID 0009-0006-6999-0729](https://orcid.org/0009-0006-6999-0729))
