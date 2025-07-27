# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies and TA-Lib C library
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
    && rm -rf /var/lib/apt/lists/*

# Build and install TA-Lib C library
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && make && make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# Set environment path for TA-Lib
ENV LD_LIBRARY_PATH=/usr/lib

# Copy your app code
COPY . .

# Install Python TA-Lib wrapper (prebuilt .whl for Python 3.12)
RUN pip install --no-cache-dir \
    https://github.com/pycaret/pycaret-extra-wheels/releases/download/v0.4.0/TA_Lib-0.4.0-cp312-cp312-manylinux_2_17_x86_64.whl

# Install all other Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port if running a web server (Flask, FastAPI, etc.)
EXPOSE 8000

# Start your bot or app
CMD ["python", "main.py"]


