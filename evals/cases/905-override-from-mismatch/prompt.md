# Adversarial case 905 — override prior-value mismatch

The override asserts `change.from: closed`, but the immutable source
actually has `status: active` for the targeted feature. This case asserts
that `overrides.from_value_matches_source` rejects the mismatch rather than
silently applying the change.
