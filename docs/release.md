# Release

1. Set `ENVIRONMENT=production` and a unique `JWT_SECRET`.
2. Use Postgres and object storage. Leave `AUTO_CREATE_TABLES` off after Alembic `0001_initial` has run.
3. Set only the provider keys you intend to bill. Empty model ids stay unconfigured.
4. Build the native app with EAS (`apps/mobile/eas.json`): preview APK, then a production build. `EXPO_PUBLIC_API_URL` must be the public API origin. Do not put secrets in that variable.
5. Confirm a guest demo shows `DEMO / MOCK RESULT` and a real session without a vision key shows a failed evaluation rather than scores.
6. Confirm a paired agent that is stopped leaves the job `worker_disconnected` and does not call the cloud.
