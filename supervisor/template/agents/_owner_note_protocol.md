## OWNER_NOTE protocol

The owner can drop you a message while you are running (Control Center, Agents panel): it lands at
`<OWNER_NOTE_PATH>`. It is not a notification — nothing interrupts you — so check for it yourself at the start of
your work and again between meaningful steps (the same points where you already check `HANDOFF_REQUEST.json`).

If the file exists and you have not yet acknowledged it (no `- [ack] ...` line at its end), read it before
continuing: it may change your priorities, add a constraint, or ask you to stop. Acknowledge it by appending a
single line in exactly this form:

```
- [ack] <ISO-8601 timestamp>
```

Do not delete or rewrite the rest of the note — only append the ack line. A note you have already acknowledged is
done; keep working unless a new, unacknowledged note appears later in the same run. If the file does not exist,
there is nothing to do.
