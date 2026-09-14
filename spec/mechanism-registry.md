# Interference mechanism registry

Mechanisms are the second component of a `primary_rc` identifier of the form
`layer.mechanism`. Layers are a closed set, revised only by specification
revision. Mechanisms are an open registry: this file.

**Current registry version: `taxonomy_version: 1`**

Every conforming dataset declares `taxonomy_version`.

## Registered mechanisms

| Identifier | Layer | Description |
|---|---|---|
| `sw.os.nonpreempt_path` | `sw.os` | Outlier time accrues in a non-preemptible kernel section, lock, or RCU path. |
| `hw.cache.llc_eviction` | `hw.cache` | Victim working set evicted from shared last-level cache by aggressor capacity pressure. |
| `hw.mmu.tlb_pressure` | `hw.mmu` | Translation buffer pressure; page-walk cost attributable to aggressor footprint. |
| `hw.irq.ipi_displacement` | `hw.irq` | Inter-processor interrupt activity displacing victim execution. |
| `hw.dma.master_traffic` | `hw.dma` | Traffic from a non-CPU master (NPU, GPU, DMA, ISP) contending for a shared path. |

These five are seed entries, chosen because they are the mechanisms the
specification's design anticipates encountering first. None has yet been
observed by an MCIB measurement campaign; they are registered as expected
instances, not as findings.

## Adding a mechanism

A port MAY introduce an extension mechanism named `x-<name>` within an existing
layer without registration. It becomes a registration candidate once a second,
independent port uses it — the rule of two.

To propose registration, open a pull request adding a row above, with:

- the identifier and its layer,
- a one-sentence mechanism description that is technology-neutral,
- the two ports that observed it and how each attributed it.

A mechanism that only one port has ever seen stays an extension. This keeps the
registry from accumulating silicon-specific entries that never recur.

## Notes on layer assignment

If a mechanism plausibly belongs to two layers, it belongs to the one where the
*time accrues*, not the one that *caused* it. An NPU saturating memory
bandwidth such that a victim stalls on a cache miss is `hw.cache` or `hw.mem`
by this rule, with the NPU's role captured separately in the aggressor's
`bottleneck_class`.

*Copyright 2026 Linaro Ltd. SPDX-License-Identifier: CC-BY-4.0*
