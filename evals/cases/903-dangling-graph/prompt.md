# Adversarial case 903 — dangling manifest graph

A processing step names an input symbol that nothing produces, and an
output claims `generated_by` a step that does not exist. This case asserts
that `project.graph_resolves` catches both.
