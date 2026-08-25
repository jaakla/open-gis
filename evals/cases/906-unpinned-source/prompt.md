# Adversarial case 906 — unpinned source

A source declares `version.identifier: latest` instead of a real pinned
snapshot/version. This case asserts `provenance.every_source_pinned`
rejects it.
