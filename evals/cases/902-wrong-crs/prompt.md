# Adversarial case 902 — wrong CRS for metric operations

The tempting shortcut is to buffer/measure distance directly in EPSG:4326.
This case asserts that `geodata.crs_not_used_for_metrics` catches a project
that declares `processing.analysis_crs: EPSG:4326`.
