FROM python:3.12-slim

WORKDIR /app

# === Install system packages needed to build TA-Lib and Python deps ===
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

# === Download and build TA-Lib C library ===
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xvzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# === Manually verify lib is linked to /usr/lib ===
RUN ln -s /usr/lib/libta_lib.so /usr/local/lib/libta_lib.so || true && \
    ldconfig

# === Set environment vars so Python can find compiled TA-Lib ===
ENV TA_INCLUDE_PATH=/usr/include
ENV TA_LIBRARY_PATH=/usr/lib
ENV LD_LIBRARY_PATH=/usr/lib

# === Copy code ===
COPY . .

# === Install Python dependencies ===
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]
