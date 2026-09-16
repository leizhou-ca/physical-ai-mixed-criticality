# Campaign Plan #2: ROS-Native Evidence — OUTLINE

**Status:** Outline. Activation gated on the reference-platform campaign
reaching its bring-up exit criteria.
**Governed by:** MCIB Specification. The phase-aware EVT requirement
(§1.4) and the `bottleneck_class` schema field (§2) are inherited by
reference — ROS-native victim measurements and any autoregressive inference
aggressor runs are subject to the §1.4 phase decomposition requirement.
**Author:** Lei Zhou, Linaro

---

## Objective

Produce ROS-native interference characterisation data — data captured
from running ROS 2 components, not projected from POSIX primitives —
sufficient for a ROSCon 2027 (or ROS-Industrial / ROSCon regional)
submission and a conforming public dataset. For assessor use, this
campaign's output is subject to the tool qualification disclaimer in
Spec §4.2, inherited by reference.

The distinction this campaign exists to enforce: interference behaviour
*projected* from POSIX primitives onto a ROS 2 graph is a hypothesis. Only
data captured from running ROS components can settle it. Until this campaign
produces that data, MCIB makes no claim about ROS-native interference.

## Scope sketch

1. **Victim (Spec family F2):** ROS 2 Jazzy executor graph — callback
   latency, executor response, and chain/path latency on a
   representative control graph (Nav2-class chain and/or the Campaign
   #1 Scenario-C pipeline), captured via **ros2_tracing** tracepoints
   with **CARET** chain analysis where the fork-build constraint allows.
   Open question: CARET requires building against its `rclcpp` fork, so a
   feasibility and overhead check is the first task.
2. **Attribution underneath:** MCIB PMU + trace attribution correlated
   with the ros2_tracing timeline — the "why + verdict" layer under
   the community's "where" layer. Composition contract: integrate,
   never fork or reimplement. This is a binding project constraint.
3. **Aggressors:** SmolVLA-class CPU inference plus calibrated synthetic
   equivalents, per Spec §1.2.
4. **Hypotheses to test** (stated as hypotheses, not expected results):
   (a) a per-callback verdict — tune / partition / ceiling — can be produced
   for a live graph; (b) interrupt-displacement effects are confined to the
   SMP cluster and do not cross an AMP boundary; (c) shared-cache eviction is
   the dominant cross-domain mechanism, with a partitioning countermeasure
   measurably bounding it. Any of these may fail. A failed hypothesis with
   attribution is a conforming result.
5. **Standards anchor:** IEC 61508 Part 3 non-interference framing
   (robotics/industrial), complementing Campaign #1's ISO 26262
   anchoring.
6. **Deliverables:** a conforming public dataset; a `ros2 launch`-level entry
   point so the tool is usable without knowing its internals; and written
   prior-art positioning against ros2_tracing, CARET, Autoware_Perf, and
   rtla.

## Dependencies

- Reference-platform bring-up complete, with the core loop running on it.
- Rule of two: ROS integration enters the product core only if a second
  campaign needs it. Until then it is campaign code.

## How to help

This is the part of MCIB most in need of people who actually run ROS 2 in
production. Useful contributions before activation:

- Tell us the CARET fork-build constraint is worse (or better) than assumed.
- Nominate a representative control graph. The choice of victim graph shapes
  everything downstream and should not be made by one person.
- Say which of the three hypotheses above you think is wrong, and why.

*Copyright 2026 Linaro Ltd. SPDX-License-Identifier: CC-BY-4.0*
