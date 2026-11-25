def main() -> None:
    """命令行入口：uv run contract-agent 启动服务，默认 http://127.0.0.1:8300。"""
    import uvicorn

    uvicorn.run("contract_agent.api:app", host="127.0.0.1", port=8300)
