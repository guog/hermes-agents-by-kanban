ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent@sha256:3dcdb7b88092fc18846ba0276a84ccf86cfd9afebc83ed8f98cdd0e78bf46e69
FROM ${HERMES_BASE_IMAGE}

USER root

ARG TARGETARCH
ARG GLAB_VERSION=1.108.0
ARG LARK_CLI_VERSION=1.0.72
ARG GITLAB_SKILLS_REV=933cee89fbbec511241cae0914e5112feda29ab2
ARG LARK_SKILLS_REV=d6cebd6723eb80e9e5761d34ccea9ab71e2f5a8d

RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) glab_arch=amd64 ;; \
      arm64) glab_arch=arm64 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${glab_arch}.tar.gz" -o /tmp/glab.tar.gz; \
    tar -xzf /tmp/glab.tar.gz -C /usr/local bin/glab; \
    chmod 0755 /usr/local/bin/glab; \
    rm /tmp/glab.tar.gz

RUN npm install --global "@larksuite/cli@${LARK_CLI_VERSION}"

RUN set -eu; \
    mkdir -p /opt/fleet/vendor/gitlab /opt/fleet/vendor/lark; \
    git clone --quiet --filter=blob:none --no-checkout https://gitlab.com/gitlab-org/ai/skills.git /tmp/gitlab-skills; \
    git -C /tmp/gitlab-skills checkout --quiet "${GITLAB_SKILLS_REV}" -- skills/glab; \
    cp -a /tmp/gitlab-skills/skills /opt/fleet/vendor/gitlab/; \
    git clone --quiet --filter=blob:none --no-checkout https://github.com/larksuite/cli.git /tmp/lark-cli; \
    git -C /tmp/lark-cli checkout --quiet "${LARK_SKILLS_REV}" -- skills/lark-shared skills/lark-im; \
    cp -a /tmp/lark-cli/skills /opt/fleet/vendor/lark/; \
    rm -rf /tmp/gitlab-skills /tmp/lark-cli

COPY --chown=hermes:hermes . /opt/fleet
COPY scripts/container-bootstrap.sh /etc/cont-init.d/018-hermes-sdd-fleet
RUN chmod 0755 /etc/cont-init.d/018-hermes-sdd-fleet /opt/fleet/scripts/*.sh
