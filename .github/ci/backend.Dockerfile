# Test tools extend the production dependency graph without changing its image.
ARG BACKEND_IMAGE
FROM ${BACKEND_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir httpx==0.28.1
USER 10001:10001
