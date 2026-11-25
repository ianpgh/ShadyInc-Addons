FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git lua5.1 \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/bin/luac5.1 -v
WORKDIR /app
COPY publishing.py server.py ./
COPY web ./web
ENV PYTHONDONTWRITEBYTECODE=1 PORT=8080
USER 65534:65534
EXPOSE 8080
CMD ["python", "server.py"]
