@echo off
title 启动 SteamDT 服务

echo 正在启动 Callback Server...
start "Callback Server" cmd /k "cd /d F:\steamdt-project && python callback_server.py"

echo 正在启动 Send Request...
start "Send Request" cmd /k "cd /d F:\steamdt-project && python send_request.py"

echo 两个服务已启动，请查看对应的 CMD 窗口。
pause