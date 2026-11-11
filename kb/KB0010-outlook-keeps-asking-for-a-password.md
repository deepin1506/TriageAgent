---
id: KB0010
title: Outlook keeps asking for a password
category: software
group: Application Support
tags: outlook, email, password prompt, credentials
---

# Outlook keeps asking for a password

## Symptoms

Outlook prompts for a password every few minutes even though the password is correct. Webmail works.

## Likely cause

Stale cached credentials or a corrupt Outlook profile, often after a password change.

## Resolution

1. Remove saved Office entries from Windows Credential Manager.
2. Sign out of Office apps and sign back in.
3. If prompts continue, create a new Outlook mail profile.

## When to escalate

Escalate to Application Support if a new profile does not fix it. If the account is also locked, see the lockout article and involve Identity & Access Management.
