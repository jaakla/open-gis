# Work with an uncertain POI source

The POI provider does not publish enough information to establish that the
download is complete. Treat this as an explicit data-quality limitation rather
than claiming the source is complete.

Document the uncertainty and its mitigation in the project and validation
results, propagate the limitation to the run status, and surface it clearly in
the presentation as a machine-readable warning. The result must remain a
warning, not a successful validation.
