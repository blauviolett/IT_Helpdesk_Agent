---
kb_id: KB-1002
title: "VPN keeps disconnecting — client-side troubleshooting"
status: VERIFIED
applies_to: [vpn, network, network_vpn]
updated: 2026-05-12
---

# VPN keeps disconnecting — client-side troubleshooting

## Symptoms

- VPN session drops every 10–20 minutes and auto-reconnects.
- Internal tools intermittently unreachable while on VPN.

## Common causes and fixes

1. **Outdated VPN client.** Versions older than 5.2 have a known keepalive bug.
   Update to the latest client from the software portal.
2. **Wi-Fi power saving.** On laptops, disable Wi-Fi adapter power management
   (OS network settings → adapter options → power management).
3. **Router/ISP idle timeout.** Switch the client protocol from UDP to TCP in
   VPN client → Settings → Connection.
4. **Split-tunnel DNS conflicts.** Flush DNS cache and reconnect.

## Verify

After each step, keep a session open for 30 minutes; the drop pattern should
disappear rather than lengthen.

## Escalate when

- VPN gateway status page shows degradation (not a client problem).
- Disconnects persist after steps 1–4 on an up-to-date client.
