FROM --platform=linux/arm64 python:3.13-slim
WORKDIR /app
COPY pyproject.toml main.py ./
RUN pip install --no-cache-dir -e .
EXPOSE 8080
CMD ["python", "main.py"]
