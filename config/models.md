# Model arms

Seven agent models, eight repetitions per cell. The arms are defined in `config/arms.json`;
the default file reproduces the seven arms used in the recordings (`make test` checks this).

| Arm | Model id (as recorded) | Adapter kind | Settings (recorded in `model_config`) |
|---|---|---|---|
| GLM-4.7-Flash | `zai-org/GLM-4.7-Flash` | openai-compatible | temperature 1.0, max_tokens 4096 |
| Qwen3-Coder-30B | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | openai-compatible | temperature 1.0, max_tokens 4096 |
| gemma-4-31B | `google/gemma-4-31B` | openai-compatible | temperature 1.0, max_tokens 4096 |
| GLM-5.2-FP8 | `zai-org/GLM-5.2-FP8` | openai-compatible | temperature 1.0, max_tokens 4096 |
| Llama-3.3-70B | `meta-llama/Llama-3.3-70B-Instruct` | openai-compatible | temperature 1.0, max_tokens 4096 |
| claude-haiku-4-5 | `claude-haiku-4-5-20251001` | anthropic-sdk | not settable through the SDK |
| claude-sonnet-5 | `claude-sonnet-5` | anthropic-sdk | not settable through the SDK |

**Per attempt:**
- An openai-compatible arm may use up to 8 tool-calling rounds.
- An anthropic-sdk arm may use up to 8 turns.

**Recorded dollars:**
- Anthropic-SDK arms record USD per trial.
- The openai-compatible arms were served by a grant-billed endpoint and record tokens but
  no price.
- So only the two SDK arms appear in the dollar table; the effort table (probes,
  invocations, retries) covers all seven.

**Tool surface:** the agent reaches the storage service through a fixed set of operations:
placement, deletion, distribution read, transfer read and transfer list. The deadline is part
of each trial's recorded contract.
