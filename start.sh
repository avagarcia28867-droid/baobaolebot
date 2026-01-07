#!/bin/bash

# 后台启动机器人
python bot.py &

# 后台启动监控
python monitor.py &

# 前台启动Web后台 (这样容器就不会退出)
# Fly.io 默认暴露 8080 端口，所以这里指定端口
uvicorn admin:app --host 0.0.0.0 --port 8080