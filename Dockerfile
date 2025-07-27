FROM python:3.12-slim

WORKDIR /app

# Install build essentials for pip + numpy
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy code
COPY . .

# Install Python dependencies (use talib-binary instead of TA-Lib)
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]
