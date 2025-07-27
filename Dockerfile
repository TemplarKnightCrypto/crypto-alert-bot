FROM python:3.12-slim

WORKDIR /app

# === Install system dependencies ===
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
    libtool \
    automake \
    && rm -rf /var/lib/apt/lists/*

# === Build and install TA-Lib C library ===
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr/local && \
    make && \
    make install && \
    ldconfig && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# Let linker find TA-Lib
ENV LD_LIBRARY_PATH=/usr/local/lib
ENV LIBRARY_PATH=/usr/local/lib
ENV CPATH=/usr/local/include

# Copy your bot files
COPY . .

# Install Python dependencies (ta-lib will link to the now-available system lib)
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]
