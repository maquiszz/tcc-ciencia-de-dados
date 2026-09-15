FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system spa \
    && useradd --system --gid spa --create-home --home-dir /home/spa spa

# Copia somente o runtime. O database.env e outros arquivos locais nunca são
# incorporados à imagem; as credenciais entram exclusivamente por --env-file.
COPY --chown=spa:spa app.py ./
COPY --chown=spa:spa database ./database
COPY --chown=spa:spa static ./static

USER spa

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=4).read()"]

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "60", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
