# Case 005 — reproducibility after exploratory correction

Perform the same basic accessibility analysis as case 001. Assume the
analyst initially tried a different (buggy) area threshold, discovered it
excluded a clearly qualifying parcel during review, and corrected
`project.yaml`/`pipeline.py` accordingly before accepting the run.

Task: the *accepted* final project (not the exploratory attempt) must be
fully reproducible: delete all derived outputs, rerun the pipeline in a
clean workspace with no access to the original chat, and confirm the
outputs and validation report reproduce exactly (excluding explicitly
nondeterministic metadata such as timestamps).
