# Correct a field-surveyed POI status

The supplied POI layer contains a kiosk whose status is recorded as active,
but a field survey found that it closed on 2026-08-20.

Apply this correction as a documented project override. Keep the supplied
source data immutable, verify the previous value before changing it, and
record useful evidence, author, and timestamp details. The effective POI
output should show the kiosk as closed, and the run should make clear that the
correction was applied.
