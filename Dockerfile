FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Công cụ mà các pregrade.sh gọi (sqlite3, editcap/tcpdump, unzip, strings)
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 tshark tcpdump unzip binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 grader
COPY --chown=grader:grader app /workspace/app
COPY --chown=grader:grader start.sh /workspace/start.sh
RUN chmod +x /workspace/start.sh \
    && mkdir -p /workspace/app/.local/pregrade /workspace/app/tmp \
    && chown -R grader:grader /workspace/app
USER grader

EXPOSE 5000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')"
CMD ["/workspace/start.sh"]
