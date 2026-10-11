# Pairing

1. Sign in with a registered account. Guests cannot register a GPU server.
2. In GPU servers, register a name. The API returns a device token once and a public id such as `SC-…`.
3. The same action issues a short pairing code. Codes expire (`PAIRING_TTL_SECONDS`, default 10 minutes), work once, and lock out after 8 failures.
4. On the GPU PC, run `streetcloud-agent setup` with that device token, then `connect`. The agent calls the API. It does not open a port for the phone.
5. Enter the public id and pairing code in the app if the server was registered from another account. The owner who just registered is already the owner. Pairing grants another account `run_generation` and `view_worker_status`.
6. Unpair revokes that account's grant. It does not delete the device token. Register again to rotate the token.
