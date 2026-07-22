FROM python:3.12-slim

LABEL description="Middleware API-Odoo"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py odoo_universal.py ./
COPY core ./core
COPY routers ./routers

ENV PORT=8000

EXPOSE 8000

# Las variables de entorno (ODOO_URL, API_KEY, etc.) se inyectan en runtime:
# docker run --env-file .env -p 8000:8000 api-odoo
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
