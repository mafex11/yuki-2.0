"""Bring your own AI: each of Yuki's jobs (voice replies, doing things, memory upkeep) on the provider and model the
user picks in Settings → AI, with Claude as before by default.

Modules (each imports only what it needs, so importing the package costs nothing):

* ``catalog``: the providers and what is known about their models (tools, size, images, price).
* ``config``: ``ai.json``, the per-role choice (``route(role)``: ``None`` = default, the existing path untouched).
* ``keys``: the keys, DPAPI-encrypted on this PC (environment variables honoured).
* ``openai_compat``: the Anthropic Messages surface over the OpenAI chat-completions protocol.
* ``client``: the client per provider; the voice talker's role-aware async client.
* ``pricing``: what a routed call cost (known, OpenRouter's own, free/local 0, else unknown).
* ``discover``: each service's model list (Ollama's local models, OpenRouter's free ones) and a one-call test.

The thinker and the memory passes are routed inside ``yuki.agent.failover.FailoverClient`` (``role=``).
"""
