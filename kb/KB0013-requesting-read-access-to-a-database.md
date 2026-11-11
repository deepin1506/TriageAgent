---
id: KB0013
title: Requesting read access to a database
category: database
group: Database Administration
tags: database access, read only, sql, analytics, reporting database
---

# Requesting read access to a database

## Symptoms

An analyst needs to query a database directly, for example the sales or finance reporting database, and gets a login or permission error.

## Resolution

1. Raise a request naming the database and the schemas or tables needed.
2. Read-only access is the default; write access needs the data owner's approval.
3. Access is given through a role, never by sharing service account passwords.

## When to escalate

Database Administration fulfils approved requests; the data owner must approve access to personal or financial data.
