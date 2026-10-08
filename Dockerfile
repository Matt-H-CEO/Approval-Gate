FROM python:3.12-slim
WORKDIR /app
COPY mock_ezio.py fixtures.json ./
ENV HOST=0.0.0.0 PORT=8787
EXPOSE 8787
CMD ["python", "mock_ezio.py"]
