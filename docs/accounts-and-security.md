# Accounts, teams and security

## Sign-in sessions

Signing in creates a server-side **session** (`auth_sessions`). The API returns:

- a short-lived **access token** (JWT, default 30 minutes, `METIS_ACCESS_TOKEN_MINUTES` 1–60) that names the session (`sid`). Every request checks that the session is still active, so revocation is immediate.
- a **refresh token** that rotates on every use (`POST /api/auth/refresh`). Only its SHA-256 hash is stored. If a rotated-out refresh token is presented again, the API assumes it was stolen and revokes the whole session.

Sessions last `METIS_SESSION_DAYS` (default 30) and can be ended in four ways: `POST /api/auth/logout` (this session), `DELETE /api/auth/sessions/{id}`, `POST /api/auth/sessions/revoke-others`, or a password change (`POST /api/auth/password`), which signs out every other session. `GET /api/auth/sessions` lists active sessions with the device's user agent.

Access tokens issued before this release have no session id and are rejected; clients must sign in again once after upgrading.

## Login throttling

After `METIS_LOGIN_MAX_FAILURES` (default 10) failed sign-ins for one email within 15 minutes, further attempts return `429` with `Retry-After`, even with the right password. A successful sign-in clears the counter. Attempts are keyed by a hash of the normalised email, so mistyped addresses are not stored in clear text. Unknown emails are throttled the same way. Registration still reports an already-registered email (`409`), so account existence is not secret.

## Roles

| Action | Member | Admin | Owner |
| --- | --- | --- | --- |
| Search, ask, read citations, own conversations | ✓ | ✓ | ✓ |
| Upload and manage documents, sources and groups; view all conversations and audit events | | ✓ | ✓ |
| Rename the organisation; invite or add members; remove members | | ✓ | ✓ |
| Invite or add admins; change roles; remove admins or owners | | | ✓ |

An organisation always keeps at least one owner. The last owner cannot be demoted, removed or leave. Anyone can leave an organisation (`DELETE /api/organisations/{id}/members/{their id}`).

## Invitations

Admins create invitations with `POST /api/organisations/{id}/invitations` (`email`, `role`). The response contains a **token shown once**; the web app turns it into a link (`/invite/{token}`) to share. Only a hash is stored.

- Links expire after `METIS_INVITATION_DAYS` (default 7, max 30), work once, and only for the invited email address.
- Re-inviting an address replaces its earlier pending link. Admins can revoke a pending link.
- `POST /api/invitations/preview` shows the organisation name and role for a token without signing in; `POST /api/invitations/accept` joins the signed-in user.
- Metis does not send email. Share links through a channel you trust.

Membership changes, invitations and organisation renames are recorded in audit events. Audit events never contain tokens or passwords.

## HTTP hardening

- **API:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`, `Permissions-Policy`, and `Cache-Control: no-store` on `/api/auth/*` and `/api/invitations/*`. Set `METIS_API_DOCS=false` to hide `/docs` and `/openapi.json` in production.
- **Web app:** see [src/frontend/README.md](../src/frontend/README.md#security-model): encrypted HttpOnly cookie, CSRF tokens, strict CSP, and safe Markdown.
- **Code and dependencies:** Ruff's flake8-bandit (`S`) rules run in pre-commit and CI, and each exception is justified inline. Audit the locked dependencies with:

  ```bash
  for p in src/backend src/frontend; do
    uv export --locked --no-dev --no-emit-project --no-hashes --project $p --format requirements-txt > /tmp/reqs.txt
    uvx --from pip-audit==2.9.0 pip-audit --strict --disable-pip --no-deps -r /tmp/reqs.txt
  done
  ```

## Not included yet

These need external accounts or decisions and are deliberately out of scope: billing and plans, outbound email (verification, password reset, invitation delivery), single sign-on, and per-document permissions. Password reset currently requires an operator. Track them before onboarding external customers.

## Testing safely

`scripts/test-postgres` and `scripts/test-queue` start disposable PostgreSQL and Redis containers on random loopback ports and remove them afterwards. Integration fixtures delete rows, so never point `TEST_DATABASE_URL` at the Compose `metis` database or any database holding real data.
