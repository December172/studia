# Studia 服务器模式（多用户）。用法见 docs/USAGE.md 的「Docker 部署」。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STUDIA_HOST=0.0.0.0 \
    STUDIA_PORT=8080 \
    STUDIA_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Run unprivileged; /data is the only writable place (mount a volume there).
RUN useradd --system --uid 10001 --home-dir /data studia \
    && mkdir -p /data && chown studia:studia /data && chmod 700 /data
USER studia
# Run from the data volume: the program writes its daily log files to the working directory.
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/mode', timeout=4)"

CMD ["python", "/app/StartWork.py", "--serve"]
