# Adversarial case 908 — validation laundering

A required check (`no_null_cadastral_id`) is declared in `project.yaml`'s
`validation.required` but omitted from the actual run's
`validation/latest-report.json`. This case asserts
`validation.required_all_present` catches the omission — a required check
missing from the report must not be treated as an implicit pass.
