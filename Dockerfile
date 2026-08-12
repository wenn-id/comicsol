# python:3.11.15-slim, digest checked 2026-08-07
FROM python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS builder
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

FROM python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff
RUN groupadd --system comic-sol \
    && useradd --system --gid comic-sol --home-dir /home/comic-sol --create-home comic-sol \
    && mkdir -p /data \
    && chown comic-sol:comic-sol /data
COPY --from=builder /src/dist /tmp/dist
COPY requirements/locks/runtime-linux-x86_64.txt /tmp/runtime-lock.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/runtime-lock.txt \
    && python -m pip install --no-cache-dir --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist
USER comic-sol
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["comic-sol", "doctor", "--output-root", "/data"]
ENTRYPOINT ["comic-sol"]
CMD ["mcp", "--root", "/data"]
