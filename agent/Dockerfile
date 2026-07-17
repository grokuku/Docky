FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./agent/
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY . ./agent/

VOLUME ["/data"]

EXPOSE 8080

CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8080"]