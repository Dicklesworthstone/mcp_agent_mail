@echo off
cd /d F:\Work\ToolsApp\mcp_agent_mail
call .venv\Scripts\activate.bat
python -c "from mcp_agent_mail.cli import app; app()" serve-http
