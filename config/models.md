# Model configuration

Seven agent models, eight replicates per cell. Sampling is each provider's default;
no temperature or top-p override is set anywhere in the harness.

| Model id (as recorded) | Serving | Billing recorded in trials |
|---|---|---|
| `claude-sonnet-5` | vendor SDK | per-trial USD recorded |
| `claude-haiku-4-5-20251001` | vendor SDK | per-trial USD recorded |
| `zai-org/GLM-5.2-FP8` | shared inference endpoint | grant-billed; no per-trial price recorded |
| `zai-org/GLM-4.7-Flash` | shared inference endpoint | grant-billed; no per-trial price recorded |
| `Qwen/Qwen3-Coder-30B-A3B-Instruct` | shared inference endpoint | grant-billed; no per-trial price recorded |
| `meta-llama/Llama-3.3-70B-Instruct` | shared inference endpoint | grant-billed; no per-trial price recorded |
| `google/gemma-4-31B` | shared inference endpoint | grant-billed; no per-trial price recorded |

Only the two SDK-served models carry recorded dollars, so only those appear in the
dollar table; the effort table (probes + invocations + retries) covers all seven.

Tool surface: the agent reaches the storage service through a fixed set of read and
write operations (placement, deletion, distribution read, transfer read, transfer list).
Per-trial tool-call budget and wall-clock deadline are recorded in each trial file.
