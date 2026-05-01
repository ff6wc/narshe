# Use the official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.9-slim AS compile-image

RUN echo 'deb http://deb.debian.org/debian testing main' >> /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends -o APT::Immediate-Configure=false gcc python3-dev

RUN python -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
# Install production dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# per pythonspeed.com, we'll now get the pre-built artifacts only
FROM python:3.9-slim AS build-image
COPY --from=compile-image /opt/venv /opt/venv

# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install git, clear the empty submodule placeholder, and force-clone the engine
RUN apt-get update && apt-get install -y git
RUN rm -rf WorldsCollide && git clone https://github.com/ff6wc/WorldsCollide.git WorldsCollide

ENV PATH="/opt/venv/bin:$PATH"

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED True
# Run the web service on container startup. Here we use the gunicorn
# webserver, with one worker process and 8 threads.
# For environments with multiple CPU cores, increase the number of workers
# to be equal to the cores available.
# Timeout is set to 0 to disable the timeouts of the workers to allow Cloud Run to handle instance scaling.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 application:application