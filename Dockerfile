FROM python:3.11-slim

WORKDIR /app

# 必要なシステムパッケージのインストール
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Python依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwrightのブラウザインストール
# chromiumのみ、依存関係も含めてインストール
RUN playwright install --with-deps chromium

COPY . .

# uvicornで起動
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
