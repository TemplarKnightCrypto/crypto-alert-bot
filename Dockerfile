FROM python:3.10-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy app code
COPY . .

# Upgrade pip first
RUN pip install --no-cache-dir --upgrade pip

# Install TA-Lib Python wrapper (prebuilt .whl from GitHub)
RUN pip install --no-cache-dir \
  https://github.com/mrjbq7/ta-lib/releases/download/0.4.0/TA_Lib‑0.4.0‑cp310‑cp310‑manylinux1_x86_64.whl

# Now install rest of requirements
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]


