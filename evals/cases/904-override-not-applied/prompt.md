# Adversarial case 904 — override listed but not applied

The manifest declares a `modify_attribute` override, but the exported/
derived dataset ignores it (the source value is unchanged). This case
asserts that inspecting the actual output data catches an override the
report claims is `applied` but that was not actually reflected in the
data.
