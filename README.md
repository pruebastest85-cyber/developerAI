# DeveloperAI

DeveloperAI is a modular local agent with:

- planner and tool routing
- context management and ranking
- internet search via configurable endpoint
- permission-based tool execution

## Local SearXNG with Docker

Run the local search backend with:

```powershell
docker compose up -d
```

Then verify the endpoint with:

```powershell
curl http://localhost:8080/search?q=python
```

The agent reads the endpoint from config/settings.json.
