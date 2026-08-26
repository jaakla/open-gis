"""Command-line tooling for reproducible Open-GIS projects."""

from .validation import Check, ValidationResult, validate_project

__all__ = ["Check", "ValidationResult", "validate_project"]
__version__ = "0.1.0"
