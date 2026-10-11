FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 modeltrace && mkdir /app/runtime && chown modeltrace:modeltrace /app/runtime
COPY --chown=modeltrace:modeltrace . .
USER modeltrace
EXPOSE 10001
ENTRYPOINT ["python", "runtime_config.py"]
CMD ["gunicorn", "--bind", "0.0.0.0:10001", "--workers", "2", "--threads", "4", "--timeout", "30", "app:app"]
