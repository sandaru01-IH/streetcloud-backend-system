# Street Cloud agent notes

This repository is a native mobile app (`apps/mobile`, Expo Router routes in `app/`) and a Python control plane (`services/api`). `services/worker-agent` is the outbound GPU process.

- Do not put provider API keys in `EXPO_PUBLIC_` variables or in the mobile bundle.
- Demo and mock images must stay visibly labeled `DEMO / MOCK RESULT`.
- Do not fabricate Alexander scores. Fixture scores are allowed only on demo sessions and must say they are fixtures.
- A private GPU job stays queued or becomes `worker_disconnected`. It does not switch to a cloud provider.
- Hosted GPU is selectable only after `HOSTED_GPU_ENDPOINT` is set.
- Default persistence is SQLite plus local files. Compose services are optional.
- Do not commit `.env`, device tokens, or `apps/mobile/.env`.
