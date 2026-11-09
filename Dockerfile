FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-contribution.txt ./
RUN pip install --no-cache-dir -r requirements-contribution.txt
COPY contribution_bot.py ./
CMD ["python", "contribution_bot.py"]
