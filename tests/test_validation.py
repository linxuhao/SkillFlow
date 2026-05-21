"""Unit tests for validation.py."""

import pytest
from skillflow.validation import OutputValidator
from skillflow.exceptions import OutputValidationError


def test_import_error_on_nonexistent_module():
    with pytest.raises(ImportError):
        OutputValidator("nonexistent.module.Model")


def test_import_error_on_bad_path():
    with pytest.raises(ImportError, match="module.ClassName"):
        OutputValidator("just_a_string_without_dot")


def test_validation_with_dotted_path_resolution():
    """Validate that a dotted-path schema resolves correctly."""
    pytest.importorskip("pydantic", reason="Pydantic not installed")

    # Use a model from a package that's importable via dotted path.
    # We register a model dynamically so importlib can find it.
    import sys
    import types
    try:
        mod = types.ModuleType("_test_validation_mod")
        from pydantic import BaseModel

        class TestModel(BaseModel):
            name: str = "default"
            count: int = 0

        mod.TestModel = TestModel
        sys.modules["_test_validation_mod"] = mod

        validator = OutputValidator("_test_validation_mod.TestModel")
        # validate() returns None on success, or raises on failure
        validator.validate({"name": "hello", "count": 42})
    finally:
        sys.modules.pop("_test_validation_mod", None)


def test_validation_detects_invalid_output():
    """Validator raises OutputValidationError on schema mismatch."""
    pytest.importorskip("pydantic", reason="Pydantic not installed")

    import sys
    import types
    from pydantic import BaseModel

    try:
        mod = types.ModuleType("_test_validation_mod2")

        class StrictModel(BaseModel):
            name: str
            count: int

        mod.StrictModel = StrictModel
        sys.modules["_test_validation_mod2"] = mod

        validator = OutputValidator("_test_validation_mod2.StrictModel")
        with pytest.raises(OutputValidationError):
            validator.validate({"name": 123, "count": "not_int"})
    finally:
        sys.modules.pop("_test_validation_mod2", None)
