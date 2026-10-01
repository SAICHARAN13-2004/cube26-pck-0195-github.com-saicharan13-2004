FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --create-home --uid 10001 packguard
WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY packguard_app/ /app/

USER 10001:10001
EXPOSE 8080
CMD ["waitress-serve", "--listen=0.0.0.0:8080", "wsgi:app"]
