# 超级学习系统 - 服务器部署镜像
# 基于 Python 3.12 slim，前端由 FastAPI 静态托管，数据挂卷持久化
FROM python:3.12-slim

WORKDIR /app

# 系统级依赖（OCR/PDF 处理所需的基础库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（清华源加速）
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /tmp/requirements.txt

# 业务代码与静态资源
COPY backend /app/backend
COPY frontend /app/frontend
COPY skill.md /app/skill.md
COPY assets/app_icon.png /app/assets/app_icon.png

WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 \
    LEARN_HOST=0.0.0.0 \
    LEARN_PORT=8080 \
    LEARN_DATA_ROOT=/app/data

EXPOSE 8080
CMD ["python", "main.py"]
