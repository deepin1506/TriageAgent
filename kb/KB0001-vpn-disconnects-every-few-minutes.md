---
id: KB0001
title: VPN disconnects every few minutes
category: network
group: Network Operations
tags: vpn, remote work, disconnect
---

# VPN disconnects every few minutes

## Symptoms

The corporate VPN connects, then drops every 5 to 15 minutes. Reconnecting works, but open sessions in web apps (CRM, intranet) are lost each time. Usually reported by people working from home.

## Likely cause

Most cases come from an unstable home router, Wi-Fi power saving on the laptop, or an outdated VPN client. If several users report drops at the same time, the VPN gateway itself may be overloaded.

## Resolution

1. Update the VPN client to the current version from the Software Center.
2. Turn off Wi-Fi power saving: Device Manager, network adapter, Power Management.
3. Try a wired connection or a phone hotspot to rule out the home router.
4. Restart the laptop and reconnect.

## When to escalate

Escalate to Network Operations if drops continue on a wired connection, or if three or more users report VPN drops within an hour (possible gateway issue). Include the time of each drop.
