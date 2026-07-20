# Transcript-judge configuration

| Setting | Value |
|---|---|
| Model | `claude-sonnet-5` |
| Calls per trial | 1 (`k=1`) |
| Sampling | provider default (no override exposed) |
| Prompt | `prompts/transcript-judge-system.txt` + `prompts/transcript-judge-user-template.txt` |
| Prompt digest | `879d428` |
| Input | the trial's own record only: task, tool calls with arguments and returns, messages, final answer |
| Withheld from the judge | the independent state timeline, the injected condition, and the resolved expectation |
| Unparseable reply | recorded as such and scored as no-conclusion, never as a pass |

The same frozen prompt and digest are used for every population the judge scores.
