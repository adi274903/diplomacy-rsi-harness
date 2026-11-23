# Backend image: Rust gateway + Python harness + web UIs. No secrets are baked in; the OpenRouter key is
# passed at runtime (from GCP Secret Manager on the VM).
FROM docker.io/library/rust:1-bookworm AS gateway
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
RUN cargo build --release -p rsi-gateway

FROM docker.io/library/python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY rsi ./rsi
RUN pip install .
COPY configs ./configs
COPY playbooks ./playbooks
COPY knowledge ./knowledge
COPY web ./web
COPY deploy ./deploy
COPY --from=gateway /src/target/release/rsi-gateway /usr/local/bin/rsi-gateway
EXPOSE 8765 8770
ENTRYPOINT ["bash", "deploy/backend.sh"]
