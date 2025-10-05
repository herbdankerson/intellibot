FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && python -m nltk.downloader punkt punkt_tab

COPY src ./src
COPY db ./db

EXPOSE 8000

CMD ["uvicorn", "src.mcp_server:app", "--host", "0.0.0.0", "--port", "8000"]
