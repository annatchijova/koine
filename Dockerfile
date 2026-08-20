# koine gate service: dashboard + webhook watcher, one Cloud Run service.
# Stdlib only -- no ADK dependency tree, no Gemini credential -- so the image
# is tiny and the always-on read/trigger surface stays decoupled from the
# translation fleet (which ships separately, see Dockerfile.fleet).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY koine ./koine
RUN pip install --no-cache-dir .

# Cloud Run injects $PORT; koine.service binds it. The default CMD serves the
# demo store so a bare `gcloud run deploy` already yields a working hosted URL;
# override --args to point --source/--translation at a real repo.
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "koine.service", "--demo"]
