FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/

ENV RELAY_HOST=0.0.0.0
ENV RELAY_PORT=8080

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
