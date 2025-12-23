# 使用官方 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖 (增加了 dos2unix 用于修复 Windows 换行符问题)
RUN apt-get update && \
    apt-get install -y --no-install-recommends dos2unix && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有项目文件
COPY . .

# 🔥 关键修复：
# 1. 转换 Windows 换行符为 Linux 格式
# 2. 赋予执行权限
RUN dos2unix start.sh && chmod +x start.sh

# 暴露端口
EXPOSE 8080

# 启动命令
CMD ["./start.sh"]
