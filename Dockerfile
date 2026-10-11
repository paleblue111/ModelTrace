FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt ./
ARG PIP_INDEX_URL=https://pypi.org/simple
# pip 26.2 fixes ProtocolError bypassing download resumption (pypa/pip#14079).
# Bootstrap it before installing large wheels, with bounded retries for both.
RUN set -eu; \
    install_with_retry() { \
        attempt=1; \
        until python -m pip install --disable-pip-version-check --progress-bar off \
            --timeout 120 --retries 5 --cache-dir /tmp/modeltrace-pip-cache \
            --index-url "$PIP_INDEX_URL" "$@"; do \
            if [ "$attempt" -ge 3 ]; then return 1; fi; \
            echo "Package installation failed; retrying ($attempt/3)..." >&2; \
            attempt=$((attempt + 1)); \
            sleep 5; \
        done; \
    }; \
    install_with_retry --upgrade pip==26.2.1; \
    install_with_retry --resume-retries 10 -r requirements.txt; \
    rm -rf /tmp/modeltrace-pip-cache
RUN useradd --create-home --uid 10001 modeltrace && mkdir /app/runtime && chown modeltrace:modeltrace /app/runtime
COPY --chown=modeltrace:modeltrace . .
USER modeltrace
EXPOSE 10001
ENTRYPOINT ["python", "runtime_config.py"]
CMD ["gunicorn", "--bind", "0.0.0.0:10001", "--workers", "2", "--threads", "4", "--timeout", "30", "app:app"]
