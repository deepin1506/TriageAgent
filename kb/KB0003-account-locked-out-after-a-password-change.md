---
id: KB0003
title: Account locked out after a password change
category: access
group: Identity & Access Management
tags: password, locked out, account, reset
---

# Account locked out after a password change

## Symptoms

After a password reset or change, the user's account locks again within minutes. Email, the HR portal and other single sign-on apps all fail at once.

## Likely cause

An old password is still saved somewhere (phone mail app, mapped drive, saved Wi-Fi profile) and keeps retrying, which triggers the lockout policy.

## Resolution

1. Unlock the account from the identity portal or through the Service Desk.
2. Update or remove the saved password on every device: phone mail, tablets, mapped drives, Wi-Fi.
3. Sign out and back in on the laptop.

## When to escalate

Escalate to Identity & Access Management if the account locks again after all saved passwords are updated; they can trace the source of failed sign-ins.
