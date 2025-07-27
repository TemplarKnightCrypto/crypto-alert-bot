FROM python:3.12-slim

WORKDIR /app

# === Install system dependencies ===
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    curl \
    gcc \
    make \
    libtool \
    libffi-dev \
    libbz2-dev \
    libssl-dev \
    zlib1g-dev \
    automake \
    && rm -rf /var/lib/apt/lists/*

# === Build and install TA-Lib C library ===
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xvzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr/local && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# === Manually register shared lib for linker ===
RUN ldconfig

# === Add TA-Lib paths to environment ===
ENV LD_LIBRARY_PATH="/usr/local/lib"
ENV LIBRARY_PATH="/usr/local/lib"
ENV CPATH="/usr/local/include"
ENV TA_LIBRARY_PATH="/usr/local/lib/libta_lib.so"

# === Copy bot code ===
COPY . .

# === Install Python packages ===
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]
