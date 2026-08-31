# llm_cache

Every LLM response, keyed by `sha256(prompt)[:16]`, committed to the repo.

This directory is why the demo cannot fail because of a rate limit, a network
blip, or a provider outage: smoke rung S5 runs the propose-verify path with no
client configured at all and asserts it resolves entirely from here.

`MANIFEST.json` records which client populated the cache and which provider
actually answered. **Read it before quoting any cost or throughput figure.**

Repopulate with `make warm-cache`.

## Rotating a provider key does not require a rebuild

Entries are keyed by `sha256(prompt)`, not by credential, so a new key reads
the same cache. `make warm-cache --rebuild` re-rolls every model response at
temperature 0.7, which puts every committed result back in play — whether
hc13 still resolves as ambiguous, the resolution count, the rejection rate,
the retry figure. Rebuild only when the prompt or the model changes, and
re-verify those four afterwards.
