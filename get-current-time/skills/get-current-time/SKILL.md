---
name: get-current-time
description: Gets the current time using whatever tools are available. Use this skill whenever you need to know the current time or date — at the start of sessions, when timestamping entries, or when the user asks what time it is. Always use this skill rather than guessing or relying on system context alone.
---

# Get Current Time

Use this skill to reliably get the current local time whenever you need it.

## Tool Priority

Try each method in order, stopping as soon as one succeeds.

### 1. user_time_v0 (preferred)

If a tool called `user_time_v0` is available in your tool list, call it. It returns the user's local time directly. Use that result and stop here.

### 2. bash_tool

If `bash_tool` is available, run:

```bash
date
```

Parse the output for the current local time and date.

### 3. Desktop Commander

If Desktop Commander tools are available, use `Desktop Commander:execute_command` (or equivalent) to run `date`.

### 4. Windows-MCP

If running on Windows (Windows-MCP tools are available), use:

```powershell
Get-Date
```

### 5. Fallback

If none of the above are available, use the date from your system context (e.g. today's date from the system prompt or conversation) and note that the exact time is unavailable.

---

## Output

After getting the time, don't announce the tool you used or narrate the process. Just use the time naturally in your response — weave it in as context, log it quietly, or state it directly if the user asked.
