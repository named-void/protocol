FROM rust:1.93.1 AS chef
RUN cargo install cargo-chef
WORKDIR app

# -----------------------------

FROM chef AS planner
COPY . .
RUN cargo chef prepare  --recipe-path recipe.json

# -----------------------------

FROM chef AS builder
RUN apt-get update && apt-get install -y \
      && rm -rf /var/lib/apt/lists/*
COPY --from=planner /app/recipe.json recipe.json
# Build dependencies - this is the caching Docker layer!
RUN cargo chef cook --release --recipe-path recipe.json
# Build application
COPY . .

RUN cargo build --release --bin oxympgs

# -----------------------------

FROM debian:trixie-slim AS runtime
RUN apt-get update && apt-get install -y \
      libssl3 ca-certificates \
      && rm -rf /var/lib/apt/lists/*
WORKDIR app
COPY --from=builder /app/target/release/oxympgs /usr/local/bin
ENTRYPOINT ["/usr/local/bin/oxympgs"]
