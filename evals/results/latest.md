# Eval report (gemini:gemini-3.1-flash-lite, 2026-11-10T21:17:12+00:00)

40 labelled tickets, run end to end with automatic approval.

| Metric | Value |
|---|---|
| Category accuracy | 0.95 |
| Impact accuracy | 0.975 |
| Urgency accuracy | 0.925 |
| Priority accuracy (derived) | 0.9 |
| Assignment group accuracy | 0.925 |
| Tool calls exactly right | 0.825 |
| Ticket correct in ServiceNow afterwards | 0.825 |
| Relevant KB article retrieved | 1.0 |
| Citations that are relevant | 0.97 |
| Proposals citing sources not retrieved | 0.0 |
| Injection attempts flagged | 1.0 |
| Clean tickets wrongly flagged | 0.0 |
| Injected tickets still routed correctly | 1.0 |
| Latency p50 (s) | 13.58 |
| Latency p95 (s) | 71.9 |
| Runs that crashed | 0 |
| ...of which the LLM provider was down | 0 |
| Tickets re-run after a provider error | 0 |

## Misses

| Ticket | Predicted | Debatable label |
|---|---|---|
| E006 | network / Network Operations / impact 2, urgency 1 |  |
| E011 | software / End User Computing / impact 3, urgency 2 |  |
| E012 | hardware / End User Computing / impact 3, urgency 2 |  |
| E013 | access / Identity & Access Management / impact 3, urgency 2 |  |
| E021 | access / Identity & Access Management / impact 3, urgency 3 |  |
| E022 | database / Database Administration / impact 1, urgency 2 |  |
| E037 | inquiry / Service Desk: L1 / impact 3, urgency 3 |  |
