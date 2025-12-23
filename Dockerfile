# 使用官方 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有项目文件
COPY . .

# 给启动脚本执行权限
RUN chmod +x start.sh

# 暴露端口 (Fly.io 内部端口)
EXPOSE 8080

# 启动命令
CMD ["./start.sh"]