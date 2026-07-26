---
kb_id: KB-1001
title: "Okta account locked out — self-service unlock"
status: VERIFIED
applies_to: [okta, account_auth]
updated: 2026-06-30
---

# Okta account locked out — self-service unlock

## Symptoms

- "Your account has been locked due to too many failed sign-in attempts."
- Password reset completes but sign-in still fails immediately.

## Cause

Okta locks an account after 5 consecutive failed attempts. A password reset does
**not** clear an active lockout — the lock must expire (30 min) or be cleared via
a self-service unlock verification email.

## Resolution steps

1. Confirm the account state is `LOCKED_OUT` (not `SUSPENDED` or `DEPROVISIONED`).
2. Trigger the self-service unlock verification email to the user's registered
   address. <!-- section: s2 -->
3. Ask the user to click the verification link within 15 minutes, then sign in
   with their **current** password.
4. If sign-in still fails after unlock, check for an expired password and advise
   a reset **after** the unlock completed.

## Escalate when

- Account state is `SUSPENDED` or `DEPROVISIONED` (requires IT admin action).
- Lockout recurs 3+ times in 24h (possible credential stuffing — security queue).
