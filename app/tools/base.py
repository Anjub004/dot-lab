"""Tool interface and registry.

Every tool:

* declares a ``name``, a ``description`` and a Pydantic ``input_model``
  (exposed as ``input_schema`` JSON Schema),
* implements ``execute(args)`` returning a :class:`ToolResult`,
* may require human approval for specific inputs via ``approval_reason(args)``,
* may report itself unavailable (e.g. missing API key) via ``availability()``,
* declares a ``permission`` level and may set its own ``timeout_seconds``.

Permission levels, from least to most risky:

* ``read``: only reads data inside the workspace (calculator, CSV tools),
* ``write``: creates or changes files in the workspace,
* ``external``: talks to the outside world (web search, browser, webhooks).

``ToolRegistry(max_permission=...)`` hides and blocks every tool above the
configured level (``TOOL_PERMISSION_LEVEL``), so the planner never sees it and
the executor refuses it even if a plan names it.

Tools raise :class:`ToolError` for expected failures. ``retryable=True`` marks
transient problems (timeouts, network errors) that the executor may retry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, Field, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"

    @property
    def rank(self) -> int:
        return _PERMISSION_RANK[self]


_PERMISSION_RANK = {PermissionLevel.READ: 0, PermissionLevel.WRITE: 1, PermissionLevel.EXTERNAL: 2}


class ToolError(Exception):
    """An expected tool failure with a message that is safe to show users."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolConfigurationError(ToolError):
    """The tool cannot run because required configuration is missing."""


class ToolInputError(ToolError):
    """The tool received invalid input."""


class ToolResult(BaseModel):
    """Result of a tool execution.

    ``content`` is a text rendering that is given to the LLM; ``data`` holds
    the structured result for the dashboard and for other code.
    """

    content: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolContext(BaseModel):
    """Information about the calling task, passed to every tool execution."""

    task_id: str | None = None
    step_id: str | None = None


class ToolAvailability(BaseModel):
    available: bool
    reason: str | None = None


class BaseTool(ABC):
    """Base class for all tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    # Tools that do not declare a level are treated as the most risky.
    permission: ClassVar[PermissionLevel] = PermissionLevel.EXTERNAL
    # Tool-specific time limit; the effective limit is min(this, TOOL_TIMEOUT_SECONDS).
    timeout_seconds: ClassVar[float | None] = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def availability(self) -> ToolAvailability:
        """Override when a tool depends on configuration."""
        return ToolAvailability(available=True)

    def approval_reason(self, args: BaseModel) -> str | None:
        """Return a reason string if this specific call needs human approval."""
        return None

    def parse_input(self, raw: dict[str, Any] | str) -> BaseModel:
        """Validate raw input (dict or JSON string) against ``input_model``."""
        try:
            if isinstance(raw, str):
                return self.input_model.model_validate_json(raw)
            return self.input_model.model_validate(raw)
        except ValidationError as exc:
            raise ToolInputError(f"Invalid input for tool '{self.name}': {exc}") from exc

    def typed(self, args: BaseModel, model: type[ModelT]) -> ModelT:
        """Narrow ``args`` to the tool's concrete input model."""
        if not isinstance(args, model):
            raise ToolInputError(f"Tool '{self.name}' expected {model.__name__}")
        return args

    @abstractmethod
    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        """Run the tool with validated input."""

    def describe(self) -> dict[str, Any]:
        availability = self.availability()
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": ToolResult.model_json_schema(),
            "permission": self.permission.value,
            "timeout_seconds": self.timeout_seconds,
            "available": availability.available,
            "unavailable_reason": availability.reason,
        }


class ToolRegistry:
    """Holds the tools the agent may use."""

    def __init__(
        self,
        tools: list[BaseTool] | None = None,
        max_permission: PermissionLevel = PermissionLevel.EXTERNAL,
    ) -> None:
        self._tools: dict[str, BaseTool] = {}
        self.max_permission = max_permission
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def permitted(self, tool: BaseTool) -> bool:
        """True if the tool's permission level is allowed by TOOL_PERMISSION_LEVEL."""
        return tool.permission.rank <= self.max_permission.rank

    def get(self, name: str) -> BaseTool:
        """Return a permitted tool, or raise ToolError (unknown or not permitted)."""
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool '{name}'") from exc
        if not self.permitted(tool):
            raise ToolError(
                f"Tool '{name}' needs '{tool.permission}' permission, but "
                f"TOOL_PERMISSION_LEVEL is '{self.max_permission}'"
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def available(self) -> list[BaseTool]:
        """Tools the planner may use: permitted and configured."""
        return [t for t in self._tools.values() if self.permitted(t) and t.availability().available]

    def describe(self) -> list[dict[str, Any]]:
        return [{**t.describe(), "permitted": self.permitted(t)} for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)
