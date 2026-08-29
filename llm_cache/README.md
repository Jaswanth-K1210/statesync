# llm_cache

Every LLM response, keyed by `sha256(prompt)[:16]`, committed to the repo.

This directory is why the demo cannot fail because of a rate limit, a network
blip, or a provider outage: smoke rung S5 runs the LLM path with the network
disabled and asserts it resolves entirely from here.

Empty until Phase 5. The harness that fills it (`src/statesync/llm/cache.py`)
was built in Phase 1 deliberately — a cache retrofitted after the fact records
whatever the network happened to return on the last run.
