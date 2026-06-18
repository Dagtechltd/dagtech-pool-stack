# SPDX-License-Identifier: MIT
# DagTech Credit Rebalancer — fixes asic-pool inverse-difficulty weighting bug
FROM python:3.12-slim
LABEL org.opencontainers.image.title="dagtech-credit-rebalancer"
LABEL org.opencontainers.image.vendor="DagTech Ltd"
LABEL org.opencontainers.image.source="https://github.com/Dagtechltd/dagtech-pool-stack"
RUN pip install --no-cache-dir 'psycopg[binary]==3.3.4'
COPY rebalancer.py /app/rebalancer.py
WORKDIR /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/rebalancer.py"]
