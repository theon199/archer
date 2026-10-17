"""Plugin Registry and Execution Dispatcher."""

import logging
from typing import Any

from app.plugins.base import Plugin
from app.plugins.files import ApprovedFilesPlugin
from app.plugins.notes import PersonalNotesPlugin
from app.plugins.web_search import WebSearchPlugin

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Manages active tool plugins and handles authorized dispatch."""

    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        # Register standard starter plugins for V1
        self.register(ApprovedFilesPlugin())
        self.register(PersonalNotesPlugin())
        self.register(WebSearchPlugin())

    def register(self, plugin: Plugin) -> None:
        self._plugins[plugin.name] = plugin
        logger.info(f"Registered plugin: {plugin.name}")

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "schema": p.schema,
            }
            for p in self._plugins.values()
        ]

    async def dispatch(
        self,
        tool_name: str,
        account_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        plugin = self.get(tool_name)
        if not plugin:
            return {
                "success": False,
                "error": f"Unknown or unapproved tool: {tool_name}",
            }

        try:
            return await plugin.execute(account_id=account_id, arguments=arguments)
        except Exception as e:
            logger.error(f"Error executing plugin {tool_name}: {e}")
            return {"success": False, "error": str(e)}


registry = PluginRegistry()
