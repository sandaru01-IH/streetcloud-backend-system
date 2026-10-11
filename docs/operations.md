# Operations

- Health: `GET /health` reports the database.
- Logs: API logs to stdout. The agent appends to `~/.streetcloud/agent.log`.
- Retention: `RETENTION_DAYS` defaults to 90. Deleting a session removes stored objects for that session. Deleting an account revokes refresh tokens and deletes owned sessions.
- Jobs: one active generation per session. A newer idea while a job is running is stored and started after the current job finishes.
- Workers offline longer than `WORKER_OFFLINE_SECONDS` (45) stop receiving a healthy status. Expired leases return the job to `worker_disconnected`.
- Secrets: `JWT_SECRET`, provider keys, `HOSTED_GPU_API_KEY`, and the agent device token. None of these belong in the mobile app.
