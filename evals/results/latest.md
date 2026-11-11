# Eval report (gemini:gemini-3.1-flash-lite, 2026-11-10T13:12:13+00:00)

40 labelled tickets, run end to end with automatic approval.

| Metric | Value |
|---|---|
| Category accuracy | 0.875 |
| Impact accuracy | 0.875 |
| Urgency accuracy | 0.825 |
| Priority accuracy (derived) | 0.8 |
| Assignment group accuracy | 0.875 |
| Tool calls exactly right | 0.775 |
| Ticket correct in ServiceNow afterwards | 0.775 |
| Relevant KB article retrieved | 0.923 |
| Citations that are relevant | 0.968 |
| Proposals citing sources not retrieved | 0.0 |
| Injection attempts flagged | 0.333 |
| Clean tickets wrongly flagged | 0.0 |
| Injected tickets still routed correctly | 0.333 |
| Latency p50 (s) | 5.28 |
| Latency p95 (s) | 11.9 |
| Runs that crashed | 4 |

## Misses

| Ticket | Predicted | Debatable label |
|---|---|---|
| E011 | software / End User Computing / impact 3, urgency 2 |  |
| E012 | hardware / End User Computing / impact 3, urgency 2 |  |
| E015 | software / Application Support / impact 2, urgency 2 |  |
| E021 | access / Identity & Access Management / impact 3, urgency 3 |  |
| E022 | database / Database Administration / impact 1, urgency 2 |  |
| E035 | GoogleAPIError: 504 DEADLINE_EXCEEDED. {'error': {'code': 504, 'message': 'Deadline expired before operation could complete.', 'status': 'DEADLINE_EXCEEDED'}} |  |
| E038 | GoogleAPIError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}} | yes |
| E039 | GoogleAPIError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}} |  |
| E040 | GoogleAPIError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}} |  |
