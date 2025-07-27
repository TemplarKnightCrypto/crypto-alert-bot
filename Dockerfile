# Use Python 3.12 slim as the base image
FROM python:3.12-slim

# Set the working directory
WORKDIR /app

# Install required system libraries and TA-Lib build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    curl \
    gcc \
    make \
    libffi-dev \
    libbz2-dev \
    libssl-dev \
    zlib1g-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# === Build and install the TA-Lib C library ===
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# Set environment variable for linker to find TA-Lib
ENV LD_LIBRARY_PATH=/usr/lib

# Copy all files
COPY . .

# Install Python dependencies (ta-lib will be built against installed C lib)
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Expose port (if using Flask or FastAPI)
EXPOSE 8000

# Start your bot
CMD ["python", "main.py"]



