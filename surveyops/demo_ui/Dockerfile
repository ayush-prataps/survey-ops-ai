FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx psycopg2-binary pydantic
COPY surveyops /app/surveyops
COPY eval /app/eval
ENV PYTHONPATH=/app
EXPOSE 8050
CMD ["uvicorn", "surveyops.demo_ui.app:app", "--host", "0.0.0.0", "--port", "8050"]
