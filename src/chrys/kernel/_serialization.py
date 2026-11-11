# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned serialization primitives."""

from __future__ import annotations

import copy
import json
import logging
import math
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)

ClassT = TypeVar("ClassT", bound="SerializationMixin")
ProtocolT = TypeVar("ProtocolT", bound="SerializationProtocol")

# Regex pattern for converting CamelCase to snake_case
_CAMEL_TO_SNAKE_PATTERN = re.compile(r"(?<!^)(?=[A-Z])")


@runtime_checkable
class SerializationProtocol(Protocol):
    """Dictionary serialization contract for kernel objects.

    ``Message`` and ``FunctionTool`` implement it through ``SerializationMixin``.
    Nested protocol instances are converted recursively by the mixin.

    Example::

        from chrys.kernel.types import Message

        message = Message(role="user", contents=["Hello"])
        restored = Message.from_dict(message.to_dict())
        assert restored.text == "Hello"
        assert restored.role == "user"
    """

    def to_dict(self, **kwargs: Any) -> dict[str, Any]:
        """Convert the instance to a dictionary.

        Keyword Args:
            kwargs: Additional keyword arguments for serialization.

        Returns:
            Dictionary representation of the instance.
        """
        ...

    @classmethod
    def from_dict(cls: type[ProtocolT], value: MutableMapping[str, Any], /, **kwargs: Any) -> ProtocolT:
        """Create an instance from a dictionary.

        Args:
            value: Dictionary containing the instance data (positional-only).

        Keyword Args:
            kwargs: Additional keyword arguments for deserialization.

        Returns:
            New instance of the class.
        """
        ...


def is_serializable(value: Any) -> bool:
    """Check if a value is JSON serializable.

    This function tests whether a value can be directly serialized to JSON
    without custom encoding. It checks for basic Python types that have
    direct JSON equivalents.

    Args:
        value: The value to check for JSON serializability.

    Returns:
        True if the value is one of the basic JSON-serializable types
        (str, int, float, bool, None, list, dict), False otherwise.

    Note:
        This function only checks for direct JSON compatibility. Complex objects
        that implement ``SerializationProtocol`` require conversion via ``to_dict()``
        before JSON serialization.
    """
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


class SerializationMixin:
    """Serialize object fields to dictionaries and reconstruct through the constructor.

    Nested protocol instances are serialized recursively. Constructors must
    accept mappings for nested objects they reconstruct. ``DEFAULT_EXCLUDE``
    omits configured fields, and ``INJECTABLE`` keeps runtime dependencies out
    of serialized data so callers can supply them to ``from_dict``.

    Example::

        from chrys.kernel.types import Message

        message = Message(role="user", contents=["Hello"])
        payload = message.to_dict()
        restored = Message.from_dict(payload)
        assert restored.text == message.text
    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = set()
    INJECTABLE: ClassVar[set[str]] = set()
    _SHALLOW_COPY_FIELDS: ClassVar[set[str]] = {"raw_representation"}

    def __deepcopy__(self, memo: dict[int, Any]) -> SerializationMixin:
        """Create a deep copy, preserving ``_SHALLOW_COPY_FIELDS`` by reference.

        Fields listed in ``_SHALLOW_COPY_FIELDS`` may contain LLM SDK objects
        (e.g., proto/gRPC responses) that are not safe to deep-copy.  They are
        kept as shallow references in the copy; all other attributes are
        deep-copied normally.
        """
        cls = type(self)
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k in cls._SHALLOW_COPY_FIELDS:
                object.__setattr__(result, k, v)
            else:
                object.__setattr__(result, k, copy.deepcopy(v, memo))
        return result

    def to_dict(self, *, exclude: set[str] | None = None, exclude_none: bool = True) -> dict[str, Any]:
        """Convert the instance and any nested objects to a dictionary.

        This method performs deep serialization, automatically converting nested
        ``SerializationProtocol`` objects, lists, and dictionaries containing
        serializable objects. Non-serializable objects are skipped with debug logging.

        Fields marked in ``DEFAULT_EXCLUDE`` and ``INJECTABLE`` are automatically
        excluded from the output, as are any private attributes (starting with '_').

        Keyword Args:
            exclude: Additional field names to exclude from serialization beyond
                    the default exclusions (``DEFAULT_EXCLUDE`` and ``INJECTABLE``).
            exclude_none: Whether to exclude None values from the output. When True,
                         None values are omitted from the dictionary. Defaults to True.

        Returns:
            Dictionary representation of the instance including a 'type' field
            for type identification during deserialization (unless 'type' is excluded).
        """
        # Combine exclude sets
        combined_exclude = set(self.DEFAULT_EXCLUDE)
        if exclude:
            combined_exclude.update(exclude)
        combined_exclude.update(self.INJECTABLE)

        # Get all instance attributes
        result: dict[str, Any] = {} if "type" in combined_exclude else {"type": self._get_type_identifier()}
        for key, value in self.__dict__.items():
            if key not in combined_exclude and not key.startswith("_"):
                if exclude_none and value is None:
                    continue
                # Recursively serialize SerializationProtocol objects
                if isinstance(value, SerializationProtocol):
                    result[key] = value.to_dict(exclude=exclude, exclude_none=exclude_none)
                    continue
                # Handle lists containing SerializationProtocol objects
                if isinstance(value, list):
                    value_as_list: list[Any] = []
                    for item in value:  # pyright: ignore[reportUnknownVariableType]
                        if isinstance(item, SerializationProtocol):
                            value_as_list.append(item.to_dict(exclude=exclude, exclude_none=exclude_none))
                            continue
                        if is_serializable(item):
                            value_as_list.append(item)
                            continue
                        logger.debug(
                            f"Skipping non-serializable item in list attribute '{key}' of type {type(item).__name__}"  # pyright: ignore[reportUnknownArgumentType]
                        )
                    result[key] = value_as_list
                    continue
                # Handle dicts containing SerializationProtocol values
                if isinstance(value, dict):
                    from datetime import date, datetime, time

                    serialized_dict: dict[str, Any] = {}
                    for raw_key, v in value.items():  # pyright: ignore[reportUnknownVariableType]
                        dict_key = str(raw_key)  # pyright: ignore[reportUnknownArgumentType]
                        if isinstance(v, SerializationProtocol):
                            serialized_dict[dict_key] = v.to_dict(exclude=exclude, exclude_none=exclude_none)
                            continue
                        # Convert datetime objects to strings
                        if isinstance(v, (datetime, date, time)):
                            serialized_dict[dict_key] = str(v)
                            continue
                        # Check if the value is JSON serializable
                        if is_serializable(v):
                            serialized_dict[dict_key] = v
                            continue
                        logger.debug(
                            f"Skipping non-serializable value for key '{dict_key}' in dict attribute '{key}' "
                            f"of type {type(v).__name__}"  # pyright: ignore[reportUnknownArgumentType]
                        )
                    result[key] = serialized_dict
                    continue
                # Directly include JSON serializable values
                if is_serializable(value):
                    result[key] = value
                    continue
                logger.debug(f"Skipping non-serializable attribute '{key}' of type {type(value).__name__}")

        return result

    def to_json(self, *, exclude: set[str] | None = None, exclude_none: bool = True, **kwargs: Any) -> str:
        """Convert the instance to a JSON string.

        This is a convenience method that calls ``to_dict()`` and then serializes
        the result using ``json.dumps()``. All the same serialization rules apply
        as in ``to_dict()``, including automatic exclusion of injectable dependencies
        and deep serialization of nested objects.

        Keyword Args:
            exclude: Additional field names to exclude from serialization.
            exclude_none: Whether to exclude None values from the output. Defaults to True.
            **kwargs: Additional keyword arguments passed through to ``json.dumps()``.
                     Common options include ``indent`` for pretty-printing and
                     ``ensure_ascii`` for Unicode handling.

        Returns:
            JSON string representation of the instance.
        """
        return json.dumps(self.to_dict(exclude=exclude, exclude_none=exclude_none), **kwargs)

    @classmethod
    def from_dict(
        cls: type[ClassT], value: MutableMapping[str, Any], /, *, dependencies: MutableMapping[str, Any] | None = None
    ) -> ClassT:
        """Reconstruct an object, validating its type and injecting runtime dependencies.

        ``dependencies`` is keyed by serialized type identifier. Within each type,
        keys name injectable parameters; ``field:value`` keys select dependencies
        for matching instances. Mapping-valued dependencies merge into existing
        mapping parameters. Names outside ``INJECTABLE`` log a diagnostic but
        are still injected.
        A supplied ``type`` that differs from the class identifier raises ``ValueError``.

        Example::

            from chrys.kernel.tools import FunctionTool

            async def echo(text: str) -> str:
                return text

            original = FunctionTool(name="echo", description="Echo text", func=echo)
            payload = original.to_dict()
            restored = FunctionTool.from_dict(
                payload, dependencies={"function_tool": {"func": echo}}
            )
            assert restored.func is echo
        """
        if dependencies is None:
            dependencies = {}

        # Resolve the expected identifier from the class itself (matching what
        # to_dict emits); resolving from the payload would make the mismatch
        # check below compare the payload against itself and never fire.
        type_id = cls._get_type_identifier()

        if (supplied_type := value.get("type")) and supplied_type != type_id:
            raise ValueError(f"Type mismatch: expected '{type_id}', got '{supplied_type}'")

        # Create a copy of the value dict to work with, filtering out the 'type' key
        kwargs = {k: v for k, v in value.items() if k != "type"}

        # Process dependencies using dict-based structure
        type_deps = dependencies.get(type_id, {})
        for dep_key, dep_value in type_deps.items():
            # Check if this is an instance-specific dependency (field:name format)
            if ":" in dep_key:
                field, name = dep_key.split(":", 1)
                # Only apply if the instance matches
                if kwargs.get(field) == name and isinstance(dep_value, dict):
                    # Apply instance-specific dependencies
                    for raw_param_name, param_value in dep_value.items():  # pyright: ignore[reportUnknownVariableType]
                        param_name = str(raw_param_name)  # pyright: ignore[reportUnknownArgumentType]
                        if param_name not in cls.INJECTABLE:
                            logger.debug(
                                f"Dependency '{param_name}' for type '{type_id}' is not in INJECTABLE set. "
                                f"Available injectable parameters: {cls.INJECTABLE}"
                            )
                        # Handle nested dict parameters
                        if (
                            isinstance(param_value, dict)
                            and param_name in kwargs
                            and isinstance(kwargs[param_name], dict)
                        ):
                            kwargs[param_name] = {**kwargs[param_name], **param_value}
                        else:
                            kwargs[param_name] = param_value
            else:
                # Regular parameter dependency
                if dep_key not in cls.INJECTABLE:
                    logger.debug(
                        f"Dependency '{dep_key}' for type '{type_id}' is not in INJECTABLE set. "
                        f"Available injectable parameters: {cls.INJECTABLE}"
                    )
                # Handle dict parameters - merge if both are dicts
                if isinstance(dep_value, dict) and dep_key in kwargs and isinstance(kwargs[dep_key], dict):
                    kwargs[dep_key] = {**kwargs[dep_key], **dep_value}
                else:
                    kwargs[dep_key] = dep_value

        return cls(**kwargs)

    @classmethod
    def from_json(cls: type[ClassT], value: str, /, *, dependencies: MutableMapping[str, Any] | None = None) -> ClassT:
        """Create an instance from a JSON string.

        This is a convenience method that parses the JSON string using ``json.loads()``
        and then calls ``from_dict()`` to reconstruct the object. All dependency injection
        capabilities are available through the ``dependencies`` parameter.

        Args:
            value: The JSON string containing the instance data (positional-only).
                   Must be valid JSON that deserializes to a dictionary with a 'type' field.

        Keyword Args:
            dependencies: A nested dictionary mapping type identifiers to their injectable dependencies.
                         See :meth:`from_dict` for detailed structure and examples of the three
                         injection patterns (simple, dict parameter, and instance-specific).

        Returns:
            New instance of the class with any specified dependencies injected.

        Raises:
            json.JSONDecodeError: If the JSON string is malformed.
            ValueError: If the parsed data doesn't contain a valid 'type' field.
        """
        data = json.loads(value)
        return cls.from_dict(data, dependencies=dependencies)

    @classmethod
    def _get_type_identifier(cls, value: Mapping[str, Any] | None = None) -> str:
        """Get the type identifier for this class.

        The type identifier is used in serialized data to enable proper deserialization.
        It follows a priority order to determine the identifier:

        1. If ``value`` contains a 'type' field, return that value (payload-supplied)
        2. If the class has a ``type`` attribute, use that value (instance-level)
        3. If the class has a ``TYPE`` attribute, use that value (class-level constant)
        4. Otherwise, convert the class name to snake_case as fallback

        ``from_dict`` calls this WITHOUT ``value`` so the identifier comes from
        the class and the payload's 'type' can be validated against it.

        Args:
            value: Optional mapping containing serialized data that may have a 'type' field.

        Returns:
            Type identifier string used for serialization and dependency injection mapping.
        """
        if value and (type_ := value.get("type")) and isinstance(type_, str):
            return type_  # type:ignore[no-any-return]
        # for todict when defined per instance
        if (type_ := getattr(cls, "type", None)) and isinstance(type_, str):
            return type_  # type:ignore[no-any-return]
        # for both when defined on class.
        if (type_ := getattr(cls, "TYPE", None)) and isinstance(type_, str):
            return type_  # type:ignore[no-any-return]
        # Fallback and default
        # Convert class name to snake_case
        return _CAMEL_TO_SNAKE_PATTERN.sub("_", cls.__name__).lower()


def make_json_safe(obj: Any) -> Any:
    """Recursively convert an object to a JSON-serializable form.

    Handles dataclasses, Pydantic models, objects with ``to_dict``/``dict``/``__dict__``,
    datetimes, lists, dicts, and primitives.  Falls back to ``str()`` for any remaining
    non-serializable value so that ``json.dumps`` never raises a ``TypeError``.

    Args:
        obj: Object to make JSON safe.

    Returns:
        A JSON-serializable version of the object.
    """
    return _make_json_safe(obj, depth=0)


def _make_json_safe(obj: Any, *, depth: int) -> Any:
    if depth > 20:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return _make_json_safe(asdict(obj), depth=depth + 1)  # type: ignore[arg-type]
    if callable(getattr(obj, "model_dump", None)):
        try:
            return _make_json_safe(obj.model_dump(), depth=depth + 1)  # type: ignore[no-any-return]
        except TypeError:
            pass
    if callable(getattr(obj, "to_dict", None)):
        try:
            return _make_json_safe(obj.to_dict(), depth=depth + 1)  # type: ignore[no-any-return]
        except TypeError:
            pass
    if callable(getattr(obj, "dict", None)):
        try:
            return _make_json_safe(obj.dict(), depth=depth + 1)  # type: ignore[no-any-return]
        except TypeError:
            pass
    if isinstance(obj, Enum):
        # A plain Enum member would otherwise fall to ``vars()`` below and walk
        # its class (``__objclass__``): megabytes of JSON for one value.
        return _make_json_safe(obj.value, depth=depth + 1)
    if isinstance(obj, Mapping):
        return {str(key): _make_json_safe(value, depth=depth + 1) for key, value in obj.items()}  # type: ignore[misc]
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(item, depth=depth + 1) for item in obj]  # type: ignore[misc]
    if isinstance(obj, (set, frozenset)):
        return [_make_json_safe(item, depth=depth + 1) for item in sorted(obj, key=repr)]  # type: ignore[misc]
    if hasattr(obj, "__dict__"):
        return {key: _make_json_safe(value, depth=depth + 1) for key, value in vars(obj).items()}  # type: ignore[misc]
    return str(obj)
