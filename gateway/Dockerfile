# The Endpoint Picker built from this module (P3.1): upstream llm-d-router v0.11.0's Dockerfile.epp, with this module in
# place of upstream's source tree. Built only by infra/picker-image.sh, with gateway/ as the build context.
# Upstream's build image, pinned by digest as every image here (versions.md). Its GOTOOLCHAIN=local keeps the build on
# this Go, which go.mod's go 1.26.6 allows.
FROM golang:1.26.8@sha256:eb36c1664dd974cde625f736e02c204383deebe03977365caaec5bf49f794348 AS build

WORKDIR /workspace
# The modules first, so a change to the code reuses this layer from Docker's cache.
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# As upstream: static, stripped (-s -w), and stamped with the release the runner logs at start and exports as its info
# metric. That is the llm-d-router release go.mod requires, v0.11.0 at commit a5cbe60.
RUN CGO_ENABLED=0 go build \
    -ldflags="-s -w -X github.com/llm-d/llm-d-router/version.CommitSHA=a5cbe600ebade00cf3e9885beaf2bfacddeabce1 -X github.com/llm-d/llm-d-router/version.BuildRef=v0.11.0" \
    -o /out/epp ./cmd/picker

# Upstream's runtime image: CA certificates, no shell. Pinned by digest.
FROM gcr.io/distroless/static:nonroot@sha256:25ac8f0c3b2334a9eb05452266bd4697864a8c8ef0ef71e5e6480d7f92868329

COPY --from=build /out/epp /app/epp
USER 65532:65532
# As upstream: gRPC (ext-proc), health, metrics, and the KV-events socket.
EXPOSE 9002 9003 9090 5557
ENTRYPOINT ["/app/epp"]
