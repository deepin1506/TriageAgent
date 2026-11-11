---
id: KB0004
title: MFA prompt not arriving on the phone
category: access
group: Identity & Access Management
tags: mfa, authenticator, push notification, two-factor
---

# MFA prompt not arriving on the phone

## Symptoms

When signing in, the multi-factor authentication (MFA) push notification never arrives on the phone, so the user cannot complete sign-in.

## Likely cause

The authenticator app lost its registration (new phone, app reinstalled), notifications are disabled, or the phone clock is out of sync.

## Resolution

1. Open the authenticator app and use the one-time code instead of the push.
2. Check that notifications are allowed for the app and the phone has data.
3. If the phone was replaced, the MFA method must be re-registered.

## When to escalate

Escalate to Identity & Access Management to reset the MFA registration after verifying the user's identity. Never reset MFA based on an email request alone.
