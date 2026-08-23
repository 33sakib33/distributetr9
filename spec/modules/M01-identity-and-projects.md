# M01 — Identity and projects

## Goal

University users can sign in, own or join projects, and be authorized on Portal API routes. Jobs always belong to a user and a project.

## In scope

- OIDC login (university IdP)
- Dev-mode header auth
- User record upsert on first login
- Projects and membership (`owner` | `member`)
- Admin flag
- Disable user

## Out of scope

- Fine-grained RBAC beyond admin vs member
- Billing
- Guest / anonymous submit

## Dependencies

None.

## Unlocks

M02 job creation (needs `user_id` + `project_id`).

## Contracts

Tables: `users`, `projects`, `project_members`.

Portal: session cookie after OIDC. `GET /me`, `POST /projects`, `GET /projects`, `POST /projects/{id}/members`.

Authorization helper: `require_project_member(user, project_id)` and `require_admin`.

## Behavior

1. First OIDC login creates `users` from `sub`, email, name.
2. User can create a project; creator is `owner`.
3. Owners add members by university email (lookup or invite-on-first-login).
4. Disabled users get 403 on mutating routes.
5. Dev auth: if `DEV_AUTH=1`, `X-Dev-User: alice@uni.edu` upserts a user. Forbidden when unset.

## Failure modes

- IdP down: login page shows a clear error; existing sessions still work until expiry.
- Duplicate project name for same owner: 409.

## Implementation steps

1. Add OIDC config (`issuer`, `client_id`, `client_secret`, redirect).
2. Session middleware (HTTP-only cookie, 12h).
3. Migrations for users/projects/members.
4. CRUD endpoints.
5. Tests: member can read, non-member 404/403, admin flag, disabled user.

## Acceptance criteria

- A university login produces a user row keyed by `university_sub`.
- A user creates a project and adds a member; the member can `GET` that project.
- Without `DEV_AUTH`, the dev header is ignored.
- `is_admin` is not settable via Portal API (seed/CLI only).
