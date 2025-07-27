# Use a lightweight base Python image
FROM python:3.12-slim

# Install system dependencies (only what's needed)
RUN apt-get update && \
    apt-get install -y gcc && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy your project files
COPY . .

# Upgrade pip and install Python dependencies
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Expose your app's port (if using Flask)
EXPOSE 8000

# Run your main script
CMD ["python", "main.py"]




