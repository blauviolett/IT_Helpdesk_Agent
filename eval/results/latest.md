# L2 Golden Eval — 实测结果

- 运行时间:2026-07-28T19:07:48+00:00
- 模型:MAIN=qwen3.7-plus / SMALL=qwen3.7-flash-2026-07-15
- 结果:**5/5 PASS**

| ID | 场景 | 结果 | outcome | escalated(reason) | 工具调用 | 成本 USD | 各轮耗时 s |
|---|---|---|---|---|---:|---:|---|
| GC-01 | 账号锁定完整闭环 | PASS | RESOLVED_BY_AGENT | False | 5 | 0.0064 | 8.5 / 0.0 / 0.0 |
| GC-02 | Salesforce 区域事故告知 | PASS | INFORMED_KNOWN_INCIDENT | False | 3 | 0.0057 | 6.0 / 0.0 |
| GC-03 | VPN 模糊描述经澄清后 GUIDED 解决 | PASS | RESOLVED_BY_AGENT | False | 3 | 0.0061 | 2.9 / 5.8 / 0.0 |
| GC-04 | 新人权限申请整体升级(POLICY_REQUIRED) | PASS | ESCALATED | True(POLICY_REQUIRED) | 2 | 0.0031 | 6.5 |
| GC-05 | Jenkins+Tableau 多系统故障正确升级 | PASS | ESCALATED | True(LOW_CONFIDENCE) | 4 | 0.0042 | 9.1 |

## 汇总(本次运行实测)

- 每轮延迟 p50 / p95:4.4s / 9.4s(共 10 轮)
- 总成本:$0.0256;工具调用总数:17
