FROM debian:bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

# Use matching Debian GDAL libraries and Python bindings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-venv python3-gdal \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv venv --python /usr/bin/python3 --system-site-packages .venv \
    && uv sync --locked --no-dev --no-cache

COPY main.py downloader.py utils.py ./
COPY gee ./gee
COPY gcld ./gcld
COPY stac ./stac
COPY cdse ./cdse
COPY shared ./shared

# Check the CLI and GDAL array bindings without contacting a backend.
RUN python -c "from osgeo import gdal, gdal_array; import gee" \
    && python main.py --help

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
