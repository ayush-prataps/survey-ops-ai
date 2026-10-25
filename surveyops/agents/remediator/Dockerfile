FROM python:3.11-slim

# Install git, curl, and build tools for psycopg2 / native wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install project dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only surveyops directory (eval/ and credentials are strictly excluded)
COPY surveyops /app/surveyops

ENV PYTHONPATH=/app
ENV WORKSPACE_DIR=/workspace
ENV PYTHONUNBUFFERED=1

EXPOSE 8006

CMD ["uvicorn", "surveyops.agents.remediator.server:app", "--host", "0.0.0.0", "--port", "8006"]
