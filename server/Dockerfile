FROM python:3.12-slim

RUN pip install --no-cache-dir "websockets>=13"

WORKDIR /app
COPY web/ /app/web/
COPY server/server.py /app/server/server.py

EXPOSE 8443 8446

ENTRYPOINT ["python3", "server/server.py", "--host", "0.0.0.0"]
# 网页登录密码用 docker compose 的 AUTH_PASSWORD 环境变量传入（见 docker-compose.yml）
