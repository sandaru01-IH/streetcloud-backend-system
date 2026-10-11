# Threat model

Assets: street photographs, prompts, generated images, account passwords, device tokens, provider keys.

Boundaries:

- The phone holds only its API base and user tokens.
- Provider keys stay on the API host.
- ComfyUI is not published to the internet by this app. The agent dials out with a bearer device token.
- Image bytes for the phone use expiring HMAC links.
- Passwords are stored as Argon2 hashes. Pairing codes and device tokens are stored as SHA-256 hashes.
- Demo fixtures and mock images are labeled so they cannot be read as model output or as an Alexander evaluation.

Accepted limits for this pilot: HS256 with a shared secret, in-process job loop, and no Redis fan-out. A production host should set `ENVIRONMENT=production`, a long `JWT_SECRET`, and Postgres.
