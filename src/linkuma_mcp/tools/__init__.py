"""Tool implementations.

Each module exposes a `register(mcp, get_client)` function that binds tools to
the FastMCP server. `get_client` is a zero-arg callable returning a
LinkumaClient instance (typically a singleton built by server.py).
"""
