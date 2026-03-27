FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

EXPOSE 4000

CMD ["uvicorn", "main:socket_app", "--host", "0.0.0.0", "--port", "4000"]
