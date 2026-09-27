"""The optional "Test" button: one tiny model request (max_tokens=1) with a pasted key.

Runs off the GUI thread. ``anthropic`` is imported here, inside the call, never
at module import. Results are plain words; the key never appears in them.
"""

from __future__ import annotations

ANTHROPIC_MODEL = "claude-haiku-4-5"
BEDROCK_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def check_model_key(backend: str, key: str, region: str = "ap-south-1",
                    timeout_s: float = 20.0) -> tuple[bool, str]:
    """``(ok, message)`` after one ``max_tokens=1`` request with ``key``."""
    key = (key or "").strip()
    if not key:
        return False, "Paste a key first."
    try:
        import anthropic
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't start the test ({type(exc).__name__})."
    timeout = anthropic.Timeout(timeout_s, connect=8.0)
    try:
        if backend == "bedrock":
            client = anthropic.AnthropicBedrock(api_key=key, aws_region=(region or "").strip() or "ap-south-1",
                                                max_retries=0, timeout=timeout)
            model = BEDROCK_MODEL
        else:
            client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout)
            model = ANTHROPIC_MODEL
        client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
    except anthropic.AuthenticationError:
        return False, "That key was refused. Check you copied all of it."
    except anthropic.PermissionDeniedError:
        return False, ("This key isn't allowed to use Claude here. Check the account's model access"
                       + (" and the region." if backend == "bedrock" else "."))
    except anthropic.NotFoundError:
        return False, ("The key works, but Claude Haiku isn't available "
                       + ("in this region." if backend == "bedrock" else "on this account."))
    except anthropic.RateLimitError:
        return True, "The key works (the account is busy or at its limit right now)."
    except (anthropic.APITimeoutError, anthropic.APIConnectionError):
        return False, "Couldn't reach the service. Check your internet connection."
    except anthropic.APIStatusError as exc:
        detail = str(getattr(exc, "message", "") or "").strip()
        detail = detail.replace(key, "(your key)")[:160]
        detail = (" " + detail) if detail else ""
        return False, f"The service said no (error {exc.status_code}).{detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Something went wrong ({type(exc).__name__})."
    return True, "It works."
