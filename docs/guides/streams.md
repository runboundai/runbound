# Async and streaming

[← Docs](../README.md)

**Async clients use the same `wrap()`.** `AsyncOpenAI` and `AsyncAnthropic` are
recognized by the same shapes; an `async def create` gets an `async def`
replacement, so your `await` sites are unchanged.

```python
client = runbound.wrap(AsyncOpenAI())
```

**Streams are guarded.** A `create(stream=True)` response comes back wrapped in
a proxy that yields the provider's chunks unchanged and in order. Exactly **one
model call is recorded when the stream ends** — by exhaustion, by `close()` /
`aclose()`, or by leaving a `with` / `async with` block. Everything the proxy
does not define is delegated to the provider's own stream object.

Usage on a stream is best-effort:

- **OpenAI** sends usage on a final chunk only when the request asked for
  `stream_options={"include_usage": True}`. runbound never adds that option to
  a request it did not write, so without it a stream records as a zero-token
  step — one step, no dollars.
- **Anthropic** reports usage across `message_start` and `message_delta`, so
  streams there are priced without any extra request options.

Exhausting the stream, calling `close()`/`aclose()`, or leaving the `with`
block reports it immediately — do that in your code. Garbage collection is
the safety net for the stream you forgot, not the mechanism.

**An abandoned stream is recorded as one partial call, when it is garbage
collected — never at interpreter exit.** A stream that is never exhausted and
never closed still ends, eventually, when nothing references it any more;
runbound registers a `weakref.finalize` callback that holds no strong
reference to the stream proxy itself, so it runs exactly when the proxy is
collected, in the session that opened the stream, and reports:

- **duration** — the time actually spent streaming (last chunk seen minus
  first), not the time since the call started;
- **tokens** — output tokens from the provider's usage if any chunk carried
  it, else `ceil(chars / 4)` of the text that was actually streamed before it
  was abandoned, marked `estimated=True` so it is never mistaken for a measured
  number; zero if neither is available, and a truthful zero is trusted rather
  than skipped. Input tokens come only from usage a chunk carried (or, under
  `estimate_tokens=True`, from the request) — OpenAI sends usage on the final
  chunk alone, so an abandoned OpenAI stream records `tokens_in=0`;
- **the circuit and in-flight cap** — the slot is freed, but the call counts
  as neither a success nor a failure, so an abandoned stream can neither close
  a circuit nor open one.

A stream that *is* exhausted, closed, or exited normally still reports exactly
once, as before — abandonment accounting is the fallback for the one case that
used to go completely uncounted, not a second report on top of the first.

---
