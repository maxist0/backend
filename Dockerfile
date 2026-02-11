# Use a lightweight Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Use ENTRYPOINT to define the executable
# This allows you to pass the stage as an argument at runtime
ENTRYPOINT ["python", "app.py"]