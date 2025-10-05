FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

CMD ["uvicorn", "openai_proxy.app:app", "--host", "0.0.0.0", "--port", "5000"]
