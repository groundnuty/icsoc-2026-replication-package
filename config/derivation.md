# Condition-derivation configuration

| Setting | Value |
|---|---|
| Model | `claude-sonnet-5` |
| Calls per item | 1 (`k=1`), isolated |
| Sampling | provider default |
| Prompt | `prompts/condition-derivation.txt` (frozen before scoring) |
| Input | the task goal + operator context (template library, provider label-to-id map, write-time file metadata) |
| Withheld | the recorded contract and every scoring output |
| Scoring | exact match per field against the recorded contract |
