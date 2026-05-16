# 1. 基础环境：Python 3.11
FROM python:3.11-slim

# 2. 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 3. 设置容器内的工作目录
WORKDIR /app

# 4. 把你电脑上当前大目录里的所有文件都拷进容器
COPY . .

# 5. 设置 Python 环境变量，让两个文件夹的代码能互相找到
ENV PYTHONPATH=/app

# 6. 分别安装两个子项目的依赖（使用清华镜像加速）
RUN pip install --no-cache-dir -r ad-direction-agent/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r ad-purpose-agent/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 7. 开放 API 端口（对应 .env 里的 8080）
EXPOSE 8080

# 8. 启动主程序
CMD ["python", "ad-direction-agent/start_server.py"]