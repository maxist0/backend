# Use a lightweight Python image
FROM python:3.14-slim

# Set build arguments for environment
ARG ENVIRONMENT=prod
ENV ENVIRONMENT=${ENVIRONMENT}

# Set the working directory
WORKDIR /app

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Create logs directory with proper permissions
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs

# Copy dependency files and install via uv
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv
RUN uv sync --frozen --no-dev

# Use the project virtual environment for runtime
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

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
ENTRYPOINT ["python", "app.py"]

# Default to production if no argument is provided
CMD ["test"]