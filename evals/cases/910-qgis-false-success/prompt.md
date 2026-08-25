# Adversarial case 910 — QGIS false success

`project.qgz` exists but its layer datasource references a file that does
not exist on disk. Success must mean valid layers, not merely that a
`.qgz` file was produced. This case asserts `qgis.static_valid` rejects it
(or `not_testable` if PyQGIS runtime is unavailable — never an implicit
pass).
