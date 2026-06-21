"""
Tool registry — turns Termux Browser Pilot's existing MCP tool definitions
(src/mcp_server.py, decorated with @mcp.tool()) into the function-schema
format Groq's tool-calling API expects.

Why reuse mcp_server.py instead of hand-writing a tool list:
  - mcp_server.py already has every command (~90+) with proper docstrings
    and typed parameters — that's the exact same info an LLM tool schema
    needs, just in a different wrapper (MCP protocol vs Groq's OpenAI-style
    function calling).
  - When the repo gains new tbp commands and someone adds them to
    mcp_server.py, this agent picks them up automatically — no duplicate
    list to maintain.

FastMCP's internal registry API can change between versions, so this is
written defensively: if introspection fails for any reason, we fall back
to a small hand-written set of the most important tools (enough to do
real work) rather than crashing.
"""

import inspect
import types
from typing import get_origin, get_args, Union

# A safety-net list, used only if dynamic extraction from mcp_server.py
# fails (e.g. a future `mcp` package version changes its internals).
# Deliberately small — covers the core navigate/read/click/type/tab loop.
_FALLBACK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_goto",
            "description": "Navigate browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_text",
            "description": "Get visible text content of the current page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_find",
            "description": (
                "Find interactive elements by visible text. Returns matching "
                "elements with CSS selectors ready for clicking/typing. Use "
                "this instead of guessing selectors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_elements",
            "description": (
                "List interactive page elements by kind (links, buttons, "
                "inputs, forms, headings, selects, images), with selectors."
            ),
            "parameters": {
                "type": "object",
                "properties": {"kind": {"type": "string"}},
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into an input/textarea element by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["target", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press",
            "description": "Press a keyboard key (Enter, Tab, Escape, etc).",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the page.",
            "parameters": {
                "type": "object",
                "properties": {"direction": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for a number of seconds.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tab_new",
            "description": "Open a new browser tab. Becomes the active tab.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _python_type_to_json_schema(annotation):
    """Best-effort mapping from a Python type annotation to JSON-schema type.
    Handles plain types (str, int, ...) and common generics (Optional[X],
    X | None, list[X], dict[...]) that show up a lot in mcp_server.py's
    tool signatures (e.g. `x: int = None`, `role: str = ""`).
    """
    origin = get_origin(annotation)

    # Optional[X] / X | None — unwrap to the non-None member type.
    if origin is Union or origin is getattr(types, "UnionType", None):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if non_none:
            return _python_type_to_json_schema(non_none[0])
        return "string"

    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"

    mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
    }
    return mapping.get(annotation, "string")


def _function_to_schema(name, fn):
    """Build an OpenAI/Groq-style function schema from a Python function's
    signature + docstring. Used both for the FastMCP-wrapped tools and could
    be reused for any plain function.
    """
    sig = inspect.signature(fn)
    doc = (inspect.getdoc(fn) or "").strip()
    # First line of docstring as the short description (full docstring is
    # often long with an Args: section — keep the prompt lean).
    description = doc.split("\n\n")[0].strip() if doc else name

    properties = {}
    required = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                           inspect.Parameter.VAR_KEYWORD):
            # *args / **kwargs — FastMCP can't generate schemas for these
            # either, so just skip them rather than wrongly marking a
            # phantom "args"/"kwargs" string param as required.
            continue
        annotation = param.annotation if param.annotation is not inspect._empty else str
        json_type = _python_type_to_json_schema(annotation)
        properties[pname] = {"type": json_type}
        if param.default is inspect._empty:
            required.append(pname)

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description[:500],
            "parameters": schema,
        },
    }


def load_tool_schemas():
    """Returns (schemas, name_to_fn) where schemas is the Groq tools=[...]
    list and name_to_fn maps tool name -> the underlying async callable
    (mcp_server.py's wrapped function, already pointed at the daemon).

    Tries dynamic extraction from mcp_server.py first; falls back to the
    small hand-written list above if that fails for any reason.
    """
    try:
        from . import mcp_server as srv

        # FastMCP keeps registered tools in a ToolManager. The exact attr
        # path has been stable across recent fastmcp/mcp versions, but we
        # guard every step so a version bump degrades gracefully instead
        # of crashing the whole agent.
        tool_manager = srv.mcp._tool_manager
        registered = tool_manager._tools  # dict: name -> Tool object

        schemas = []
        name_to_fn = {}
        for name, tool in registered.items():
            fn = getattr(tool, "fn", None) or getattr(tool, "func", None)
            if fn is None:
                continue
            schemas.append(_function_to_schema(name, fn))
            name_to_fn[name] = fn

        if not schemas:
            raise RuntimeError("No tools found in mcp_server registry")

        return schemas, name_to_fn

    except Exception as e:
        print(f"[tool_registry] dynamic extraction failed "
              f"({type(e).__name__}: {e}); using fallback tool set "
              f"({len(_FALLBACK_TOOLS)} tools)")

        from .client import send_command

        # Build name_to_fn for the fallback set by wrapping send_command
        # directly (bypassing mcp_server.py entirely).
        name_to_fn = {}
        action_map = {
            "browser_goto": "goto",
            "browser_text": "text",
            "browser_find": "find",
            "browser_elements": "elements",
            "browser_click": "click",
            "browser_type": "type",
            "browser_press": "press",
            "browser_scroll": "scroll",
            "browser_wait": "wait",
            "browser_tab_new": "tab_new",
        }

        for tool_name, action in action_map.items():
            def _bind(action=action):
                async def _call(**kwargs):
                    resp = await send_command(action, kwargs)
                    if resp.get("success"):
                        return resp["data"]
                    return {"error": resp.get("error", "Unknown error")}
                return _call
            name_to_fn[tool_name] = _bind()

        return _FALLBACK_TOOLS, name_to_fn
