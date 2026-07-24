# Verified Local Dependency Surface

Verified on 24 July 2026 with Python 3.12.10.

## Installed versions

- `claude-agent-sdk==0.2.126`
- `streamlit==1.60.0`
- `openpyxl==3.1.5`
- Claude Code CLI `2.1.185`

## Claude Agent SDK options

The installed `ClaudeAgentOptions` exposes:

```text
add_dirs
agents
allowed_tools
betas
can_use_tool
cli_path
continue_conversation
cwd
debug_stderr
disallowed_tools
effort
enable_file_checkpointing
env
extra_args
fallback_model
fork_session
hooks
include_hook_events
include_partial_messages
load_timeout_ms
max_budget_usd
max_buffer_size
max_thinking_tokens
max_turns
mcp_servers
model
output_format
permission_mode
permission_prompt_tool_name
plugins
resume
sandbox
session_id
session_store
session_store_flush
setting_sources
settings
skills
stderr
strict_mcp_config
system_prompt
task_budget
thinking
tools
user
```

All options required by the architecture are present: `system_prompt`, `tools`,
`allowed_tools`, `setting_sources`, `model`, `max_turns`, `cwd`, and
`cli_path`.

The installed package also exposes the expected exception classes:
`CLINotFoundError`, `CLIConnectionError`, `ProcessError`,
`CLIJSONDecodeError`, and `ClaudeSDKError`. No SDK fallback is required.

The explicit CLI executable
`C:\Users\fk6147\AppData\Roaming\npm\claude.cmd` works. Configuration keeps
`cli_path=None` initially so SDK auto-detection is exercised in the Phase 4
subscription smoke test; the explicit path remains the documented fallback.
