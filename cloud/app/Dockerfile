# Light sensor rig: the web app. Built from the repo root, so it uses the same
# session.py, logger.py, check_session.py and mqtt_link.py as the Pi and the laptop, and runs
# the mock rig (pi_agent.py with mock_rig/) when MQTT_MOCK_PASSWORD is set.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv MPLBACKEND=Agg DATA_DIR=/data
WORKDIR /srv
COPY cloud/app/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY mqtt_link.py session.py logger.py check_session.py rig.py pi_agent.py /srv/
COPY mock_rig /srv/mock_rig
COPY cloud/app /srv/app
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
