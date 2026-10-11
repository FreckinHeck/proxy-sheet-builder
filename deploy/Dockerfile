# Build from the REPOSITORY ROOT, not from this directory — the COPY below
# reaches into server/, and a Docker build context cannot traverse "..".
#
#   docker build -f deploy/Dockerfile -t artproxy .
#
# artproxy needs nothing beyond the standard library. Pillow is optional and
# only used to downscale thumbnails; without it /thumb serves originals, which
# works but moves a great deal more data.
FROM python:3.12-slim

RUN pip install --no-cache-dir Pillow==11.0.0

COPY server/artproxy.py /app/artproxy.py

RUN mkdir -p /cache /orders

EXPOSE 8778
ENV PORT=8778 CACHE_DIR=/cache ORDERS_DIR=/orders
CMD ["python", "-u", "/app/artproxy.py"]
