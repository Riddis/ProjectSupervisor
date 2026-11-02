# Changelog

## 1.0.0

- Adapter `local_llm`, generating against an Ollama server over its HTTP API, backend-generic with no default
  model.
- A doctor check for the server and the configured model; the optional model-store drive check runs only when
  a drive is configured.

## 0.1.0

- Scaffolded by `supervisor plugin new local_llm --package` (framework 2.0.0).
