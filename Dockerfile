# Data-only path and offline check in a container, with no network:
#   docker build -t consumer-agent-sla-artifact .
#   docker run --rm --network none consumer-agent-sla-artifact
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
RUN apt-get update \
 && apt-get install -y --no-install-recommends make \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /artifact
COPY requirements-harness.txt .
RUN pip install --no-cache-dir -r requirements-harness.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["sh", "-c", "make check && make test"]
