# Build from the repo root:
#   docker build -f services/budget_guard/Dockerfile -t budget-guard .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 guard
WORKDIR /app
COPY services/budget_guard services/budget_guard
RUN pip install --no-cache-dir ./services/budget_guard

USER guard
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "budget_guard.server"]
