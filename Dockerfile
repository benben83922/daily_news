FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 確認這裡的檔名跟你的 news.py 一致
CMD ["python", "news.py"]