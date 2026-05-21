"""Optional Pydantic output validation utility.

skillflow core does NOT import this module. It is opt-in: when a
StepNode has ``output_schema`` set, confirm_step imports and uses
OutputValidator.

Validation failure produces an OutputValidationError with a
user-friendly message suitable for feeding back to an LLM for
self-correction.
"""

from __future__ import annotations

from skillflow.exceptions import OutputValidationError


class OutputValidator:
    """Validates step outputs against a Pydantic model.

    Usage::

        validator = OutputValidator("mypackage.schemas.TaskPlanOutput")
        validated = validator.validate({"plan": "...", "subtask_count": 5})
    """

    def __init__(self, schema_dotted_path: str):
        self._schema_path = schema_dotted_path
        self._model = self._load_model(schema_dotted_path)

    def _load_model(self, dotted_path: str):
        """Import a Pydantic model by dotted path.

        e.g. "aitelier.schemas.PMOutput" → imports aitelier.schemas.PMOutput
        """
        parts = dotted_path.rsplit(".", 1)
        if len(parts) != 2:
            raise ImportError(
                f"Invalid output_schema path '{dotted_path}': "
                f"must be 'module.ClassName'"
            )

        module_name, class_name = parts
        try:
            import importlib
            module = importlib.import_module(module_name)
            model = getattr(module, class_name, None)
            if model is None:
                raise ImportError(
                    f"Class '{class_name}' not found in module '{module_name}'"
                )
            return model
        except ImportError as e:
            raise ImportError(
                f"Failed to load output_schema '{dotted_path}': {e}"
            ) from e

    def validate(self, outputs: dict) -> dict:
        """Validate outputs against the Pydantic model.

        Returns the validated data (potentially coerced by Pydantic).

        Raises OutputValidationError with a user-friendly message on failure.
        """
        try:
            validated = self._model.model_validate(outputs)
            if hasattr(validated, "model_dump"):
                return validated.model_dump()
            return dict(validated)
        except Exception as e:
            raise OutputValidationError(
                f"Output validation failed against {self._schema_path}: {e}"
            ) from e
