# secrets/

The only place a credential may live. Git-ignored, except this README. Never referenced from a packet, never read by an agent, redacted
from logs (`config/policy.yaml`, framework `supervisor/policy.py`). Agent CLIs use their own subscription logins;
an API key goes here only for a lane the owner approved (e.g. `claude_api`: `ANTHROPIC_API_KEY=...` in `.env`).
