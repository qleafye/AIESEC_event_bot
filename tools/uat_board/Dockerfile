FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html ./
RUN mkdir -p /data
ENV UAT_DATA=/data/state.json
EXPOSE 8005
CMD ["python", "app.py"]
