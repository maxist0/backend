# Build dependencies in a separate stage to keep the runtime small
FROM python:3.14-slim AS builder

# Set the working directory
WORKDIR /app

# Install uv and sync dependencies into a local venv
RUN pip install --no-cache-dir uv

# Copy dependency files and install via uv: dependencies first (cached layer),
# then the app package itself (uv_build, src layout) as a regular, non-editable
# install so its metadata (APP_VERSION) is present in the venv.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage
FROM python:3.14-slim

# Set the working directory
WORKDIR /app

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Create logs directory with proper permissions
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs

# Copy the virtual environment from the builder
COPY --from=builder /app/.venv /app/.venv

# Use the project virtual environment for runtime
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

# Copy the rest of the application code
COPY . .


# Change ownership of the application files
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose the port the app runs on
EXPOSE 8000

# Health check to ensure the container is healthy
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/metrics/health')" || exit 1


# Use ENTRYPOINT to define the executable
# This allows you to pass the stage as an argument at runtime
ENTRYPOINT ["python", "run.py"]