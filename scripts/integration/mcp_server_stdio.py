import sys
import os

# Try to import the build function from the installed package
try:
    from mcp_agent_mail.app import build_mcp_server
except ImportError as e:
    print(f"Error importing mcp_agent_mail: {e}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    # Initialize and run the server using stdio transport
    mcp = build_mcp_server()
    mcp.run(transport="stdio")
