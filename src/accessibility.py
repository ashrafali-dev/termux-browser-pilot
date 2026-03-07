"""Accessibility tree access via CDP."""


class AccessibilityCommands:
    """Query the browser's accessibility tree."""

    def __init__(self, session):
        self.session = session
        self._enabled = False

    async def enable(self):
        """Enable the Accessibility domain."""
        if not self._enabled:
            await self.session.send("Accessibility.enable")
            self._enabled = True

    async def get_tree(self):
        """Get the full accessibility tree."""
        await self.enable()
        result = await self.session.send("Accessibility.getFullAXTree")
        return result.get("nodes", [])

    async def get_tree_summary(self, max_depth=3):
        """Get a simplified text summary of the a11y tree."""
        await self.enable()
        params = {}
        if max_depth is not None:
            params["max_depth"] = max_depth
        result = await self.session.send("Accessibility.getFullAXTree", params)
        nodes = result.get("nodes", [])
        lines = []
        for node in nodes[:200]:  # Limit for readability
            name_val = node.get("name", {}).get("value", "")
            role_val = node.get("role", {}).get("value", "")
            if role_val in ("none", "generic", "InlineTextBox"):
                continue
            if name_val or role_val:
                lines.append(f"[{role_val}] {name_val}")
        return "\n".join(lines)

    async def find_by_role(self, role):
        """Find all nodes matching an ARIA role."""
        nodes = await self.get_tree()
        return [
            n for n in nodes
            if n.get("role", {}).get("value") == role
        ]

    async def find_by_name(self, name):
        """Find nodes by accessible name (partial match)."""
        nodes = await self.get_tree()
        name_lower = name.lower()
        return [
            n for n in nodes
            if name_lower in n.get("name", {}).get("value", "").lower()
        ]
