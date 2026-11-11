# Eval report (gemini:gemini-3.1-flash-lite, 2026-11-10T12:37:18+00:00)

40 labelled tickets, run end to end with automatic approval.

| Metric | Value |
|---|---|
| Category accuracy | 0.925 |
| Impact accuracy | 0.925 |
| Urgency accuracy | 0.875 |
| Priority accuracy (derived) | 0.8 |
| Assignment group accuracy | 0.975 |
| Tool calls exactly right | 0.75 |
| Ticket correct in ServiceNow afterwards | 0.75 |
| Relevant KB article retrieved | 1.0 |
| Citations that are relevant | 0.969 |
| Proposals citing sources not retrieved | 0.0 |
| Injection attempts flagged | 1.0 |
| Clean tickets wrongly flagged | 0.0 |
| Injected tickets still routed correctly | 1.0 |
| Latency p50 (s) | 6.76 |
| Latency p95 (s) | 45.15 |
| Runs that crashed | 0 |

## Misses

| Ticket | Predicted | Debatable label |
|---|---|---|
| E009 | hardware / End User Computing / impact 3, urgency 2 |  |
| E011 | software / End User Computing / impact 3, urgency 2 |  |
| E012 | hardware / End User Computing / impact 3, urgency 2 |  |
| E015 | software / Application Support / impact 1, urgency 1 |  |
| E016 | inquiry / Application Support / impact 3, urgency 3 | yes |
| E021 | access / Identity & Access Management / impact 3, urgency 3 |  |
| E022 | database / Database Administration / impact 1, urgency 2 |  |
| E027 | access / Identity & Access Management / impact 3, urgency 3 |  |
| E034 | security / Security Operations / impact 2, urgency 1 |  |
| E035 | security / Security Operations / impact 1, urgency 1 |  |
