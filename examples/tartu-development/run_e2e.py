"""Convenience entrypoint for the canonical Tartu Open-GIS pipeline.

All processing, validation, QGIS generation, run metadata, and dashboard
rendering live in pipeline.py so the plain and E2E commands cannot drift.
"""

from pipeline import main


if __name__ == "__main__":
    main()
