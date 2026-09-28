FROM ubuntu:24.04

ARG CURA_VERSION=5.13.0
ARG GH_TOKEN=""

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CURA_VERSION=${CURA_VERSION} \
    CURA_ROOT=/opt/cura-extract \
    AUTO_SLICER_INPUT=/data/input \
    AUTO_SLICER_OUTPUT=/data/output \
    AUTO_SLICER_CURA_CONFIG=/data/cura-config \
    AUTO_SLICER_APP=/data/app \
    AUTO_SLICER_FRONTEND=/opt/auto-slicer/frontend

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/cura
RUN set -eu; \
    url="https://github.com/Ultimaker/Cura/releases/download/${CURA_VERSION}/UltiMaker-Cura-${CURA_VERSION}-linux-X64.AppImage"; \
    if [ -n "${GH_TOKEN}" ]; then \
        curl -fL --retry 3 -H "Authorization: Bearer ${GH_TOKEN}" -o cura.AppImage "$url"; \
    else \
        curl -fL --retry 3 -o cura.AppImage "$url"; \
    fi; \
    chmod +x cura.AppImage; \
    ./cura.AppImage --appimage-extract; \
    mkdir -p /opt; \
    mv squashfs-root /opt/cura-extract; \
    rm -rf /tmp/cura; \
    find /opt/cura-extract -name CuraEngine -type f -exec chmod +x {} \;

ARG ORCA_VERSION=2.4.2
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglu1-mesa \
        libgtk-3-0t64 \
        libmspack0t64 \
        libwebkit2gtk-4.1-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/orca
RUN set -eu; \
    url="https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"; \
    if [ -n "${GH_TOKEN}" ]; then \
        curl -fL --retry 3 -H "Authorization: Bearer ${GH_TOKEN}" -o orca.AppImage "$url"; \
    else \
        curl -fL --retry 3 -o orca.AppImage "$url"; \
    fi; \
    chmod +x orca.AppImage; \
    ./orca.AppImage --appimage-extract; \
    mkdir -p /opt; \
    mv squashfs-root /opt/orca-extract; \
    rm -rf /tmp/orca; \
    bin="$(find /opt/orca-extract -type f -iname 'orca-slicer' | head -n 1)"; \
    test -n "$bin"; \
    chmod +x "$bin"; \
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$bin" > /usr/local/bin/orca-slicer; \
    chmod +x /usr/local/bin/orca-slicer

RUN apt-get update && apt-get install -y --no-install-recommends \
        libice6 \
        libsm6 \
    && rm -rf /var/lib/apt/lists/*

ENV ORCA_VERSION=${ORCA_VERSION} \
    ORCA_ROOT=/opt/orca-extract \
    ORCA_SLICER=/usr/local/bin/orca-slicer

WORKDIR /opt/auto-slicer
COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

ENV PATH="/opt/venv/bin:${PATH}"

EXPOSE 8080
VOLUME ["/data/input", "/data/output", "/data/cura-config", "/data/app"]

CMD ["auto-slicer"]
