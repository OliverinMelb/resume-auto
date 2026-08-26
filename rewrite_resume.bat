@echo off
setlocal
cd /d "%~dp0"
python -m resume_agent.rewrite_cli %*
exit /b %ERRORLEVEL%
