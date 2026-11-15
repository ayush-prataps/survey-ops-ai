FROM python:3.11-slim

WORKDIR /app

COPY surveyops/agents/runner/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only surveyops directory (eval/ is strictly excluded)
COPY surveyops /app/surveyops

ENV PYTHONPATH=/app

EXPOSE 8005

CMD ["uvicorn", "surveyops.agents.runner.server:app", "--host", "0.0.0.0", "--port", "8005"]
