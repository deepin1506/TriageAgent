---
id: KB0012
title: Reports time out with a database query timeout
category: database
group: Database Administration
tags: report, timeout, database, query, slow, finance, month end
---

# Reports time out with a database query timeout

## Symptoms

Reports in the reporting tool fail after about 60 seconds with a 'query timeout' error. Often several users of the same report are affected, especially at month end.

## Likely cause

Blocking queries, outdated statistics or missing indexes on the reporting database, or a heavy batch job running during business hours.

## Resolution

Users cannot fix this themselves. Note the report name, the time of the failure and how many people are affected.

## When to escalate

Escalate to Database Administration. When a whole team is blocked during a critical period such as month-end close, treat it as high impact and high urgency.
