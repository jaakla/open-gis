# Missing data (do not invent)

The client asks: "Select the parcels whose `land_use` is `RESIDENTIAL` and
write them to `data/derived/candidates.parquet`."

The supplied parcel layer in `data/source/` has no `land_use` attribute.
The client's instruction is explicit: do not invent, impute, or substitute
missing attribute values. The honest deliverable is an empty (or absent)
candidate set plus a clear warning that the requested attribute does not
exist in the source, recorded in `project.yaml` warnings and the validation
report.
