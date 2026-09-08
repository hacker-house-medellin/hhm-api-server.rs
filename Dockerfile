# syntax=docker/dockerfile:1.7

FROM rust:1.94-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
COPY Cargo.toml Cargo.lock ./
RUN cargo fetch --locked
COPY . .
RUN cargo build --locked --release

FROM debian:bookworm-slim
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/hacker-house-medellin/hhm-api-server.rs" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
COPY --from=build /work/target/release/hhm-api /usr/local/bin/hhm-api
USER app
ENV HOST=0.0.0.0 \
    PORT=8080 \
    OTEL_SERVICE_NAME=hhm-api \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://dd-otel-collector.observability.svc.cluster.local:4318 \
    RUST_LOG=info
EXPOSE 8080

# The image carries only ciphertext. The age key arrives at run time and the
# entrypoint decrypts directly into the process environment before exec.
ARG SOPS_ENV=prod
COPY --chmod=0755 --from=ghcr.io/getsops/sops:v3.10.2-alpine /usr/local/bin/sops /usr/local/bin/sops
COPY --chmod=0755 scripts/sops-entrypoint.sh /usr/local/bin/sops-entrypoint.sh
COPY --chmod=0644 env/enc/${SOPS_ENV}.env.enc /app/secrets/app.env
ENV SOPS_SECRETS_FILE=/app/secrets/app.env

ENTRYPOINT ["/usr/local/bin/sops-entrypoint.sh", "/usr/local/bin/hhm-api"]
