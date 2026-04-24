FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080
# Single-threaded is fine — one test talks to this at a time.
CMD ["python", "app.py"]
