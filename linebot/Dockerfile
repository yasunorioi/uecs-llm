FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# .envは実行時にenv_fileで渡す（コンテナに含めない）
# .env.exampleは参照用のみ

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8443"]
