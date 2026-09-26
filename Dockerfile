FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY Bot.py Function.py Sniper.py Minigames.py WebDashboard.py Database.py Vars.example.py ./
COPY templates/ templates/

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

CMD ["python", "Bot.py"]