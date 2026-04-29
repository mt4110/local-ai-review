FROM python:3.12-slim

RUN python -m pip install --no-cache-dir 'setuptools<81' datasette==0.65.1

ENTRYPOINT ["datasette"]
