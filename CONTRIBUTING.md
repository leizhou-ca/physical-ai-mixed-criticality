# Contributing to MCIB

## Scope

MCIB measures and attributes interference between AI inference and real-time
control on shared silicon. It integrates with the existing ROS 2 tracing
ecosystem (`ros2_tracing`, CARET, `Autoware_Perf`) and does not fork or
reimplement it.

## What is most useful

1. **Conforming datasets from other platforms.** Measurements taken on silicon
   this project does not have access to are the highest-value contribution.
   See `spec/` for the results schema; provenance fields are mandatory.
2. **Review of the interference taxonomy.** If you have hit an interference
   channel in production that the taxonomy does not cover, open an issue
   describing the mechanism and how you observed it.
3. **ROS-native capture.** See the Campaign #2 plan in `campaigns/`.

## Claim discipline

Every factual claim in this repository is labelled **measured**, **specified**,
or **planned**. Contributions must carry the same labelling.

- *Measured* requires a dataset with provenance, reproducible from the
  documented configuration.
- *Specified* means defined in the specification but not yet exercised.
- *Planned* means intended and not yet built.

Characterisation data produced by this project is not qualified safety
evidence. Please do not add language that presents it as certification
evidence for ISO 26262, IEC 61508, or any other standard.

## Licensing of contributions

Code contributions are accepted under the Apache License 2.0. Documentation
and dataset contributions are accepted under CC BY 4.0. By opening a pull
request you confirm you have the right to contribute the material under those
terms.

## Issues

Bug reports, measurement disagreements, and taxonomy gaps are all welcome as
issues. A measurement that contradicts something published here is especially
welcome — please include your configuration.
