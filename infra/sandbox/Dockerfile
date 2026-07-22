# AI Studio — sandbox base image (architecture §8, zero trust).
#
# A deliberately minimal, NON-ROOT image for the Docker sandbox that the 🔴
# `shell` tool runs commands inside (see runtime/sandbox/docker.py). It bakes in
# NO secrets and NO credentials — secrets never reach an agent or its sandbox
# (CLAUDE.md invariant 5, ADR-0011); the runner forwards only an explicit env
# allowlist at run time.
#
# Build (on the host — Docker is not available in the remote session):
#     docker build -t ai-studio-sandbox:latest infra/sandbox
#
# The tag matches DockerSandboxRunner's default SANDBOX_IMAGE. Point SANDBOX_IMAGE
# at any other minimal image if you prefer.

FROM python:3.11-slim

# A pinned non-root user the container runs as. DockerSandboxRunner also passes
# `--user 65534:65534` (nobody) by default as belt-and-braces; this named user is
# here so an image that is run without an explicit --user still lands non-root.
RUN groupadd --system --gid 10001 sandbox \
 && useradd  --system --uid 10001 --gid sandbox --home-dir /workspace --shell /usr/sbin/nologin sandbox \
 && mkdir -p /workspace \
 && chown sandbox:sandbox /workspace

# The runner bind-mounts the scoped host workdir here and sets it as the cwd.
WORKDIR /workspace

# Drop to non-root. Combined with the runner's --read-only rootfs, --cap-drop
# ALL, --security-opt no-new-privileges and --network none, the container has no
# privileges, no network, and an immutable filesystem outside the mounted workdir
# and a tmpfs /tmp.
USER sandbox

# No ENTRYPOINT/CMD that runs anything on its own: the runner always supplies the
# command explicitly as `sh -c "<command>"`.
CMD ["sh"]
