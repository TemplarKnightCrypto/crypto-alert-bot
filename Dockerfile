FROM python:3.12-slim

# Install build tools and system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    curl \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Build and install the TA-Lib C library
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xvzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

ENV LD_LIBRARY_PATH="/usr/lib"

# Set working directory
WORKDIR /app

# Copy all project files
COPY . .

# Install Python dependencies (must include TA-Lib==0.6.4)
RUN pip install --no-cache-dir -r requirements.txt

# Expose port (optional, if using Flask)
EXPOSE 8000

# Start the app
CMD ["python", "main.py"]
