# MCIB Specification v1.3

**Status:** Draft. Normative once frozen. · **Author:** Lei Zhou, Linaro
**License:** CC BY 4.0

**Change policy:** revisions only when a campaign demonstrates necessity;
all revisions versioned with migration notes. Stability is a feature.

**Numeric status convention:** every number in this spec is tagged
`[NORMATIVE]` (conformance-binding) or `[REFERENCE]` (default; deviation
permitted with declaration in provenance). An untagged number is a drafting
error — please open an issue.

This document defines the platform-agnostic core of MCIB. The normative core
is the **profile → attribute → countermeasure** loop (Sections 1–3): it applies
to any physical-AI workload-optimization context, bare-metal RTOS through
virtualized mixed-criticality Linux, with no safety framing required. Section 4
defines **output profiles** over the core, which are OPTIONAL for conformance.
Platform bindings (BSP, performance-monitor event maps, aggressor drivers,
hypervisor configs) are campaign material and are exemplary, not normative.

**This specification has not yet been exercised against a measurement
campaign.** It is published at draft stage deliberately, so that the metric
definitions, taxonomy, and schema can be argued with before data exists to
defend. Disagreement is the contribution being solicited.

---

## 1. Metrics & Methodology

### 1.1 Victim metric families

**F1 — OS primitive latencies (POSIX-normalized).** Five primitives, defined
here so that this specification is self-contained and portable without
reference to any particular implementation. Implementations must be
cross-compilable to POSIX-compliant RTOS targets.

| ID | Definition |
|---|---|
| `TSLat` | Task switch: from a real-time thread yielding the CPU to a peer thread of equal priority beginning execution on the same core. |
| `PreLat` | Task preemption: from a lower-priority thread signalling a synchronization primitive to a higher-priority blocked thread resuming. The signalling mechanism MUST be declared (semaphore, file descriptor, or signal are the reference set); results are not comparable across mechanisms. |
| `InLat` | Interrupt handling: from a hardware event edge to receipt in the waiting task. The measurement design MUST ensure the waiter is genuinely blocked when the event fires. |
| `MuLat` | Mutex shuffling: acquisition cost for a priority-inheriting mutex transferred between workers. Core placement (same-core vs cross-core) MUST be declared; cross-core acquisition includes a coherency transaction and is a different quantity. |
| `IMLat` | Inter-task messaging: one-way IPC latency. Thread mode and process mode MUST be reported separately and MUST NOT be pooled. |

Two measurement constraints apply to all F1 metrics `[NORMATIVE]`:

- **Timing reference:** a frequency-stable, DVFS-independent counter,
  accessible without syscall overhead where the platform provides one.
- **Hot-path discipline:** the measurement loop stores raw counter deltas
  only. All conversion, filtering, and statistics are computed in a
  post-processing pass. Inline conversion is a material fraction of the
  intervals being measured and contaminates the distribution.

**F2 — Application chain latencies:** end-to-end response of a
representative control chain (e.g., periodic control loop, ROS 2
callback chain, observe-infer-act pipeline). Chain definition is
campaign-specific; reporting rules (§1.4) are not.

**F3 — Device-path latencies (paired-path method):** guest-observed
request→completion latency for a virtualized device path, always
reported against a passthrough or native baseline of the same device
under the identical stressor/aggressor matrix.

### 1.2 Aggressor & stressor taxonomy

- **Synthetic:** CPU-burn (ALU/FPU), memory pressure (LLC/TLB), block
  or network I/O (interrupt storm). Serve as the calibration reference
  scale.
- **Real inference:** CPU/GPU/NPU inference workloads at controlled,
  logged intensities. Each real aggressor SHOULD be expressed as a
  calibrated synthetic equivalent (equivalence mapping) with stated
  error bars.
- **Backend/domain load:** virtualization backend CPU burn, co-running
  domain activity.

### 1.3 Measurement environment normalization (required declarations)

A conforming run declares: compiler flags; memory locking and
pre-faulting; DVFS state; idle-state policy; CPU isolation method;
IRQ affinity policy; cache-state control (cold/warm); RT throttling
state; thermal management state. Deviations are permitted but MUST be
declared in provenance.

### 1.4 Reporting rules

- Distribution reporting: median, P99, P99.9, observed max, sample
  count. Sample count ≥ 100K per scenario `[REFERENCE — deviation
  permitted with declaration]`; 500K is the Campaign #1 reference
  configuration.
- **Floor/tail decomposition:** results separate the irreducible
  OS-path floor (software-path non-preemptibility class) from the
  contention-driven tail (attributable to aggressors). Floor share is
  established per-platform, never assumed from another platform.
- **pWCET:** tail estimates use a statistically defensible
  extreme-value method with (a) a goodness-of-fit gate, (b) a
  phase-structure characterisation and EVT applicability scope
  declaration, (c) stated confidence. pWCET fields populate only when
  both gates pass. The chosen method must be internally consistent
  (POT→GPD or block-maxima→GEV, not mixed).

  **Phase-aware measurement `[NORMATIVE]`:** for autoregressive
  inference aggressors (transformer prefill, token decode, expert
  activation), non-stationarity across execution phases is the
  expected structural condition, not an edge case (see Zheng et al.,
  arXiv:2512.12615, Dec 2025). Conforming measurements MUST
  characterise the phase-dependent structure of the aggressor workload
  and declare the measurement window or phase decomposition under which
  EVT assumptions hold. Acceptable approaches: (i) per-phase
  measurement windows with separate EVT fits per phase, or (ii) a
  declared steady-state window (e.g., decode-only after prefill
  completion) with the excluded phases documented in provenance. A
  single-window EVT fit across structurally distinct phases without
  phase decomposition declaration is non-conforming for autoregressive
  aggressors. A port MUST document its phase detection and
  windowing protocol before publishing measurements taken under an
  autoregressive aggressor.
- Reproducibility `[NORMATIVE]`: two clean-boot runs of the same
  configuration reproduce P99 within 5%.

## 2. Results Schema & Provenance (mcib-results-schema)

JSON, schema-validated. The schema is named **mcib-results-schema**
and versions itself via a mandatory `schema_version` field in every
dataset — no external identifier registry. Core fields per run:
`metric_id`, `victim_class`, `aggressor_config`, `isolation_config`,
`stressor_condition`, `samples`, `p50_us`, `p99_us`, `p999_us`,
`max_us`, `os_path_floor_us`, `contention_tail_us`, `pwcet_evt_us`,
`pwcet_confidence`, `primary_rc`, `bottleneck_class`.

**`bottleneck_class` `[NORMATIVE]`:** mandatory typed field declaring
the aggressor's primary resource binding — the resource dimension the
aggressor is saturating, which determines which countermeasure class
applies. Distinct from `primary_rc` (which attributes the victim's
interference root cause); `bottleneck_class` characterises the
*aggressor's* consumption profile. Rationale: empirical evidence
(Zheng et al., arXiv:2512.12615) demonstrates that misattribution of
bottleneck class causes wrong countermeasure class selection — applying
a scheduler policy to a memory-placement problem produces negligible
improvement. Making bottleneck class explicit and machine-readable in
the schema prevents this class of countermeasure error.

Valid values:

| Value | Aggressor resource dimension | Maps to taxonomy layers |
|---|---|---|
| `compute-bound` | ALU/FPU/tensor-core saturation | `hw.core`, `sw.runtime` |
| `memory-bound` | LLC/DRAM bandwidth or capacity saturation | `hw.cache`, `hw.mem` |
| `interconnect-bound` | NoC/AXI/CHI bus saturation | `hw.interconnect`, `hw.dma` |
| `mixed` | Two or more dimensions co-dominant | Requires `bottleneck_sub` field |

When `bottleneck_class: mixed`, a `bottleneck_sub` field is required
listing the co-dominant dimensions in descending order of contribution.
`mixed` without `bottleneck_sub` is non-conforming.

`primary_rc` is a **layered interference-channel identifier** of the
form `layer.mechanism`, not a closed enum. Rationale: certification
practice (AMC 20-193 / CAST-32A lineage) treats interference channels
as platform-identified instances of shared-resource *classes*; a spec
that enumerates instances gets forked by the first new SoC generation.

**Layers (durable, technology-neutral, closed set — revised only by
spec rev):**

| Layer | Covers |
|---|---|
| `sw.os` | Kernel/scheduler software paths: non-preemptible sections, locks, RCU, timer paths |
| `sw.hypervisor` | VM exits, stage-2 translation, virtual interrupt injection, device-model/backend paths |
| `sw.runtime` | Language/inference runtime: allocator, dispatch, JIT/GC-class effects |
| `hw.core` | Core-private microarchitecture: pipeline, branch, SMT sharing |
| `hw.cache` | Shared cache hierarchy: capacity eviction, coherence/snoop |
| `hw.mmu` | TLB pressure, page walks, IOMMU/SMMU translation |
| `hw.interconnect` | Bus/NoC contention and ordering (AXI/CHI-class) |
| `hw.mem` | Memory controller/DRAM: bank conflicts, refresh, row policy, bandwidth saturation |
| `hw.dma` | Traffic originated by non-CPU masters: NPU/GPU/DMA/ISP |
| `hw.irq` | Interrupt routing, IPIs, doorbells |
| `env` | Thermal, DVFS, power-state transitions |

**Mechanisms (open registry):** registered per layer in
`spec/mechanism-registry.md`; ports MAY introduce `x-<name>` extension mechanisms,
which become registration candidates when a second port uses them
(rule of two). Every dataset declares
`taxonomy_version`.

Example instances, to illustrate the `layer.mechanism` form:
`sw.os.nonpreempt_path`, `hw.cache.llc_eviction`, `hw.mmu.tlb_pressure`,
`hw.irq.ipi_displacement`, `hw.dma.master_traffic`. These five are the seed
entries of the mechanism registry (`spec/mechanism-registry.md`).

**Port obligation:** each platform port publishes its **interference
channel inventory** — which channels exist on that silicon/stack and
which its instrumentation can observe — as part of conformance (§5),
mirroring the per-platform channel-identification obligation in
multicore certification practice.

**Provenance (mandatory, auto-populated, never hand-entered):**
firmware/bootloader/hypervisor/kernel commit hashes and kernel config
hash; RTOS build hash where applicable; silicon revision as read from
the running system; board serial; environment normalization
declarations (§1.3); thermal/fan state at run start/end; aggressor and
isolation configuration identifiers. A result with missing or
hand-entered provenance is non-conforming.

## 3. Attribution Protocol

1. **Outlier selection:** samples beyond a *declared* threshold form
   the outlier population `[NORMATIVE: threshold must be declared]`;
   > P99.9 `[REFERENCE]`.
2. **Two-channel attribution (capability requirement, tool-agnostic):**
   - **Hardware-condition channel:** correlate outlier windows with
     shared-resource state via whatever monitors the platform exposes
     (core/cluster/uncore counters, memory-controller monitors,
     interconnect QoS counters, accelerator monitors), reporting
     enrichment ratios and correlation ranking.
   - **Software-path channel:** attribute outlier time to execution
     paths at the privilege layer where it accrues — kernel,
     hypervisor, RTOS, or runtime.

   The spec deliberately does NOT prescribe instruments.
   **Instrumentation bindings are port-owned decisions made by domain
   specialists** and documented per port with an **attribution
   coverage map**: which taxonomy layers (§2) each instrument can
   observe on that platform. Layers no instrument can observe are
   covered by the differential method (rule 3) or declared
   unattributable. Exemplary bindings: Linux/Arm → PMU via perf +
   ftrace/eBPF; Xen → xentrace/xenalyze, VM-exit accounting,
   per-domain scheduling traces; Zephyr/RTOS → CTF-class tracing +
   cycle counters; bare-metal → cycle counters + differential only;
   accelerators → vendor performance monitors where exposed.
3. **Differential attribution (foundational method):** attribution
   MUST be defensible without internal visibility into the aggressor —
   including fully closed accelerators. Reference approach:
   **differential aggressor toggling** — identical victim measured
   under calibrated synthetic vs. real-aggressor-only conditions,
   attributing the delta — corroborated where available by
   cluster-level (DSU), memory-controller, or per-core counters.
   Counter-based evidence is corroboration; the differential design is
   primary, because it survives shrinking silicon observability.
   Per-core PMU heuristics alone are not acceptable as primary
   attribution.
4. **Countermeasure verdict:** every attributed outlier class
   terminates in one of: `tune` (configuration removes it),
   `partition` (a named isolation mechanism bounds it), `ceiling` (no
   countermeasure; design around the bound). Verdict + evidence +
   selected countermeasure. A number without a verdict is a measurement, not
   an MCIB result.

## 4. Output Profiles (OPTIONAL for conformance)

Profiles render core-loop results (§§1–3) for specific consumers. A
dataset is conforming without any profile; profiles add consumers,
never alter the core.

### 4.1 Timing Manifest profile (contract format)

Per workload, per partition, machine-readable declaration:
memory footprint; bandwidth demand envelope; burst profile;
worst-case latency (floor and tail components separately, per §1.4);
precision/fallback map where applicable. Derived from measurement,
validated by replay. Consumers: admission control, enforcement
mechanisms, deployment decisions.

**Device-path transport contracts (F3):** per device class, declared
transport floor + contention-tail bound, feeding a
virtualized-vs-passthrough-vs-static decision parameterized by the
consumer's deadline class.

### 4.2 Interference Characterisation profile (for assessor use)

**Tool qualification disclaimer `[NORMATIVE]`:** MCIB uses community
BSP, upstream OSS instrumentation, and tooling that has not undergone
functional safety qualification. The outputs of this profile are
structured interference characterisation data suitable for use by
qualified assessors as inputs to their safety argumentation — they are
not qualified safety evidence as defined by ISO 26262 Part 8 clause 11,
IEC 61508 Part 6, or ISO PAS 8800. The qualification context is
supplied by the assessor's process, not by MCIB. Campaign plans inherit
this disclaimer by reference; no duplicate language is required in
campaign documents.

Renders the same attribution data in assessor-consumable form:
freedom-from-interference characterisation and characterisation data
templates for use in ISO 26262, IEC 61508 Part 3, and ISO PAS 8800
assessor argumentation. This profile consumes core-loop outputs
unchanged; it introduces no additional measurement requirements
beyond complete provenance (§2).

## 5. Conformance

**A conforming dataset:** validates against the mcib-results-schema; carries
complete auto-populated provenance; declares its environment per §1.3;
reports per §1.4 including floor/tail decomposition; attributes
outliers per §3 including countermeasure verdicts; and is regenerable
by a scripted clean-boot entry point published with the dataset.
Output profiles (§4) are optional.

**A conforming platform port** implements victim family F1 (F2/F3 as
applicable), publishes its **interference channel inventory** (§2) and
**attribution coverage map** (§3.2), binds both attribution channels
to the platform's chosen instruments, and produces conforming
datasets. Recognized port classes:
- **Linux** (PREEMPT_RT or standard) — full software-path + hardware-
  condition binding (e.g., perf/ftrace/eBPF-class instruments);
- **Hypervisor-inclusive** (Xen/KVM-class stacks) — adds
  `sw.hypervisor`-layer instrumentation (trace/exit accounting) to the
  guest-level binding;
- **RTOS** (Zephyr-class, POSIX-normalized victims) — cycle counters +
  RTOS tracing as available;
- **Bare-metal** — differential attribution only (§3.3 foundational
  method), which is sufficient for conformance: floor/tail
  decomposition from paired runs, provenance from build + board
  metadata.

**Acceleration path declaration (required):** each conforming
platform port must declare its hardware acceleration inventory — which
hardware acceleration units (e.g., GPU, NPU) are present, which have
OSS-accessible driver paths, and which instrumentation can observe
their interference channels. Ports with proprietary-only acceleration
paths must declare this explicitly and label affected datasets as
baseline-only. OSS hardware-accelerated paths are required for
non-baseline dataset publication.

Publication rules (closed-division-style comparability, certification of
scores) are the subject of a future publication-rules document and are outside
this spec.

---

*MCIB Specification v1.3.*

*Copyright 2026 Linaro Ltd. SPDX-License-Identifier: CC-BY-4.0*
