# Auth

Accounts use Argon2 password hashes and HS256 JWTs signed with `JWT_SECRET`. Access tokens last 30 minutes. Refresh tokens rotate. Production startup refuses the `dev-only` secret prefix.

The auth functions sit behind the route layer so a later OpenID Connect issuer can replace password login without changing session ownership.
