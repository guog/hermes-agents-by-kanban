ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a
FROM ${HERMES_BASE_IMAGE}

USER root

ARG TARGETARCH
ARG NODE_VERSION=22.18.0
ARG NODE_SHA256=c1bfeecf1d7404fa74728f9db72e697decbd8119ccc6f5a294d795756dfcfca7
ARG DOTNET_SDK_VERSION=8.0.423
ARG DOTNET_SHA512=e94513dfe42271a85f01e87bd4272aa80b4ec13556f4531754802542225667775242c5e281a94837dae6cc65f7bcc457d2f663f240c0e2b7573fd909e786b1a5

RUN test "${TARGETARCH:-amd64}" = "amd64" \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    node_archive="node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}"; \
    echo "${NODE_SHA256}  ${node_archive}" | sha256sum -c -; \
    tar -xJf "${node_archive}" -C /usr/local --strip-components=1; \
    rm -f "${node_archive}"; \
    node_major="$(node --version | sed -E 's/^v([0-9]+).*/\\1/')"; \
    npm_major="$(npm --version | cut -d. -f1)"; \
    test "${node_major}" -ge 22; \
    test "${npm_major}" -ge 10

RUN set -eux; \
    dotnet_archive="dotnet-sdk-${DOTNET_SDK_VERSION}-linux-x64.tar.gz"; \
    curl -fsSLo "${dotnet_archive}" \
        "https://builds.dotnet.microsoft.com/dotnet/Sdk/${DOTNET_SDK_VERSION}/${dotnet_archive}"; \
    echo "${DOTNET_SHA512}  ${dotnet_archive}" | sha512sum -c -; \
    install -d -m 0755 /usr/share/dotnet; \
    tar -xzf "${dotnet_archive}" -C /usr/share/dotnet; \
    rm -f "${dotnet_archive}"; \
    ln -sfn /usr/share/dotnet/dotnet /usr/local/bin/dotnet; \
    dotnet --list-sdks | grep -Fx "${DOTNET_SDK_VERSION} [/usr/share/dotnet/sdk]"

COPY requirements-controller.txt /opt/hollysys-controller-src/
RUN /opt/hermes/.venv/bin/pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        -r /opt/hollysys-controller-src/requirements-controller.txt

COPY container/patch-hermes-terminal.py /opt/hollysys-build/
RUN /opt/hermes/.venv/bin/python \
        /opt/hollysys-build/patch-hermes-terminal.py /opt/hermes \
    && rm -rf /opt/hollysys-build

COPY hollysys_controller /opt/hollysys-controller-src/hollysys_controller
COPY hollysysctl /usr/local/bin/hollysysctl
COPY cli /opt/cli
COPY container/git /opt/fleet/container/git
COPY container/install-git-wrapper.sh /opt/fleet/container/install-git-wrapper.sh
RUN chmod 0555 /usr/local/bin/hollysysctl \
    && sh /opt/fleet/container/install-git-wrapper.sh \
    && /opt/hermes/.venv/bin/python -m py_compile \
        /opt/hollysys-controller-src/hollysys_controller/*.py \
    && jq --version \
    && node --version \
    && npm --version \
    && dotnet --version

ENV DOTNET_ROOT=/usr/share/dotnet
ENV HERMES_SCRATCH_DIR=/opt/data/scratch
ENV PYTHONPATH=/opt/hollysys-controller-src
