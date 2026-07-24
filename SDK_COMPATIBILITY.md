# Verified Local Dependency Surface

Verified on 24 July 2026 with Python 3.12.10.

## Installed versions

- `claude-agent-sdk==0.2.126`
- `streamlit==1.60.0`
- `openpyxl==3.1.5`
- System npm Claude Code CLI `2.1.185`
- SDK-bundled native Claude Code CLI `2.1.218`

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

The installed SDK contains its supported native Windows executable at
`.venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe` and prefers it
when `cli_path=None`.

Although the npm `claude.cmd` shim works when invoked directly from PowerShell,
SDK 0.2.126 deliberately rejects `.cmd` and `.bat` values for `cli_path`.
Configuration therefore keeps `cli_path=None`. If an explicit override is ever
needed, it must point to a native `claude.exe`, never a batch shim.

## Subscription smoke result

With `ANTHROPIC_API_KEY` unset, both the inherited default model and explicit
`model="haiku"` returned exactly `OK` through `AgentSDKClient` on 24 July 2026.
The live calls required network execution outside the restricted development
sandbox; no API key was used.
