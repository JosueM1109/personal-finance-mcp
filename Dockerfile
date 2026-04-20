FROM python:3.11-slim
LABEL io.modelcontextprotocol.server.name="io.github.JosueM1109/personal-finance-mcp"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py plaid_client.py ./
EXPOSE 8000
CMD ["python", "server.py"]
