FROM python:3.12-slim
WORKDIR /app
COPY shared_cache/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared_cache /app/shared_cache
COPY service/app/models.py service/app/shared_schema.py /app/service/app/
RUN useradd --uid 10001 --create-home cache && mkdir /data && chown cache:cache /data
USER cache
ENV SHARED_CACHE_DB=/data/cache.sqlite3
EXPOSE 18766
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,json; assert json.load(urllib.request.urlopen('http://127.0.0.1:18766/health'))['ok']"
CMD ["uvicorn", "shared_cache.server:app", "--host", "0.0.0.0", "--port", "18766", "--no-access-log"]
