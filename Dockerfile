FROM python:3.11-slim

# Security: non-root user
RUN useradd -m -u 1000 firogate

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory with correct permissions
RUN mkdir -p /app/data /app/logs && \
    chown -R firogate:firogate /app

USER firogate

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
