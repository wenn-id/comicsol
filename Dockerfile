FROM python:3.11.15-slim AS builder
WORKDIR /src
COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE SKILL.md ./
COPY comic_sol_product ./comic_sol_product
COPY scripts ./scripts
COPY assets ./assets
COPY templates ./templates
COPY references ./references
RUN python -m pip install --no-cache-dir build==1.3.0 setuptools==80.9.0 \
    && python -m build --wheel

FROM python:3.11.15-slim
RUN groupadd --system comic-sol \
    && useradd --system --gid comic-sol --home-dir /home/comic-sol --create-home comic-sol \
    && mkdir -p /data \
    && chown comic-sol:comic-sol /data
COPY --from=builder /src/dist /tmp/dist
RUN python -m pip install --no-cache-dir /tmp/dist/*.whl mcp==1.28.1 \
    && rm -rf /tmp/dist
USER comic-sol
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["comic-sol", "doctor", "--output-root", "/data"]
ENTRYPOINT ["comic-sol"]
CMD ["mcp", "--root", "/data"]
