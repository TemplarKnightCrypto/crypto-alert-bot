FROM python:3.12-slim

# Install TA-Lib C library dependencies
RUN apt-get update && \
    apt-get install -y build-essential wget && \
    wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xvzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && rm -rf ta-lib*

# Set working directory
WORKDIR /app
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port and run the bot
EXPOSE 8000
CMD ["python", "main.py"]

