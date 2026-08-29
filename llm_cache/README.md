# llm_cache

Every LLM response, keyed by `sha256(prompt)[:16]`, committed to the repo.

This directory is why the demo cannot fail because of a rate limit, a network
blip, or a provider outage: smoke rung S5 runs the propose-verify path with no
client configured at all and asserts it resolves entirely from here.

`MANIFEST.json` records which client populated the cache. **Read it before
quoting any cost or throughput figure** — a cache filled by the offline
heuristic client tells you nothing about a real provider's generation cost.

Repopulate with `make warm-cache`.
