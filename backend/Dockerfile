# Local development Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Same hash-pinned install as production (plus test deps), so local dev runs
# the exact versions that ship instead of whatever resolves from the
# unpinned requirements.txt today.
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
