FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx psycopg2-binary pydantic
COPY surveyops/controller /app/surveyops/controller
ENV PYTHONPATH=/app
EXPOSE 8088
CMD ["uvicorn", "surveyops.controller.app:app", "--host", "0.0.0.0", "--port", "8088"]
