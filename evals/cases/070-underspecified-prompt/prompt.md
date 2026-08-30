# Vague request (deliberately underspecified)

"Make me a useful map of the Tartu test data."

The data layers in `data/source/` are supplied. No thresholds, no output
format, no selection rule are given. Do not silently invent decision
parameters: whatever you choose must be declared in `project.yaml`
(interpretation.assumptions, with rationale) and reflected in
`presentation.controls` so the reader can change it. Deliver the standard
project artifact with the candidate set at
`data/derived/candidates.parquet` (column `parcel_id`).
