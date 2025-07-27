FROM python:3.12-slim

WORKDIR /app

# === Install dependencies and TA-Lib from source ===
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

# === Download and build TA-Lib from source ===
RUN curl -L http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz | tar zx && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && make install && \
    cd .. && rm -rf ta-lib

# === Copy and install Python packages ===
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000
CMD ["python", "main.py"]


