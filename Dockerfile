FROM python:3.12-slim-bookworm AS runtime

# MySQL 8.4 LTS client tools (mysqldump, mysql) from the official MySQL APT repo.
# The 8.x client supports caching_sha2_password and dumps 5.7 with --column-statistics=0.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://repo.mysql.com/RPM-GPG-KEY-mysql-2025 \
        | gpg --dearmor -o /usr/share/keyrings/mysql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/mysql.gpg] http://repo.mysql.com/apt/debian bookworm mysql-8.4-lts" \
        > /etc/apt/sources.list.d/mysql.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mysql-community-client \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV GUARDIAN_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "db_guardian.app:build", "--factory", "--host", "0.0.0.0", "--port", "8000"]


FROM runtime AS test
RUN pip install --no-cache-dir ".[test]"
COPY tests ./tests
CMD ["python", "-m", "pytest", "-m", "integration", "-q"]
