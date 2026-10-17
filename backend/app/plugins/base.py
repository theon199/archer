"""Base plugin interface for Archer."""

from abc import ABC, abstractmethod
from typing import Any


class Plugin(ABC):
    """Abstract base class for all Archer plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin tool identifier."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for tool schemas."""
        pass

    @property
    @abstractmethod
    def schema(self) -> dict[str, Any]:
        """JSON schema describing the tool parameters."""
        pass

    @abstractmethod
    async def execute(self, account_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute the tool safely and return structured outcomes."""
        pass
