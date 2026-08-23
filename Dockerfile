# Single canonical base-image reference for every stage; digest checked
# 2026-08-07. The release workflow deliberately passes no base-image build
# argument so this pinned digest cannot drift from the audited value.
ARG PYTHON_BASE="python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
FROM ${PYTHON_BASE} AS builder
WORKDIR /src
COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE SKILL.md ./
COPY requirements/locks ./requirements/locks
COPY comic_sol_product ./comic_sol_product
COPY scripts ./scripts
COPY assets ./assets
COPY templates ./templates
COPY references ./references
RUN python -m pip install --no-cache-dir --require-hashes -r requirements/locks/release-linux-x86_64.txt \
    && python -m build --wheel --no-isolation

FROM ${PYTHON_BASE}
# comic-sol runs as the fixed numeric identity 10001:10001 so compose files,
# CI assertions, and host volume ownership all agree without name lookup.
RUN groupadd --gid 10001 comic-sol \
    && useradd --uid 10001 --gid 10001 --home-dir /home/comic-sol --create-home --shell /usr/sbin/nologin comic-sol \
    && mkdir -p /data \
    && chown comic-sol:comic-sol /data
COPY --from=builder /src/dist /tmp/dist
COPY requirements/locks/runtime-linux-x86_64.txt /tmp/runtime-lock.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/runtime-lock.txt \
    && python -m pip install --no-cache-dir --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["comic-sol", "doctor", "--output-root", "/data"]
ENTRYPOINT ["comic-sol"]
CMD ["mcp", "--root", "/data"]
