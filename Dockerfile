FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces require the app to run on port 7860
# Koyeb and others often use 8000 or the PORT env var
ENV PORT=7860
EXPOSE 7860

# start.sh launches the background sync worker AND the web server.
RUN chmod +x start.sh
CMD ["./start.sh"]
