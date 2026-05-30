"""
DRAGON CONTEXT ARCHITECTURE v1.0
===================================
Synthesized from production harnesses: Pi, OpenClaw, Claude Code, Letta Code

Core insight: Context is not a passive transcript. It is an actively managed
working set that must feel infinite within a fixed-size window.

This module implements ALL convergent patterns from the four major harnesses.
"""

# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 1: FILE READS
# ═══════════════════════════════════════════════════════════════
#
# All four harnesses agree on:
#   1. Hard cap file reads (2000 lines or 50KB)
#   2. offset/limit pagination tool parameters
#   3. Continuation nudge appended to truncated output
#   4. stat() before read to check size
#
# Claude Code adds: 256KB byte cap pre-read gate via stat()
# Letta Code adds: 10MB absolute max, overflow to disk
#
# Implementation rules:
#   - Default read: first 2000 lines, 50KB max
#   - Files > 50KB: auto-truncate with continuation hint
#   - Files > 256KB: reject with error pointing to grep/offset
#   - Tool description MUST explain pagination explicitly

FILE_READ_DEFAULT_LINES = 2000
FILE_READ_MAX_BYTES = 50_000       # 50 KB
FILE_READ_HARD_MAX_BYTES = 256_000  # 256 KB
FILE_READ_MAX_LINE_LENGTH = 2000    # per-line cap

FILE_READ_CONTINUATION_TEMPLATE = (
    "[Showing lines {start}-{end} of {total}. "
    "Use offset={next_start} to continue.]"
)

FILE_READ_TOOL_DESCRIPTION = """\
Read a file from the filesystem.

LIMITS:
- Output is truncated to {max_lines} lines or {max_bytes}KB, whichever hits first.
- Lines longer than {max_line_len} characters are truncated.
- Files over {hard_max}KB are rejected. Use grep or offset/limit instead.

PAGINATION:
- Use offset (1-indexed) and limit to read specific sections.
- Example: offset=2001, limit=2000 reads lines 2001-4000.
- When truncated, the output includes a continuation hint.

TRUNCATION STRATEGY:
- Head+tail: If truncation occurs, show beginning and end with middle cut.
- The tool description and continuation nudge tell the model what else is available.
"""


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 2: TOOL RESULT BUDGETS
# ═══════════════════════════════════════════════════════════════
#
# All four harnesses maintain a SEPARATE budget for tool results vs conversation.

TOOL_RESULT_MAX_CHARS = 16_000        # Per-result cap (Pi default)
TOOL_RESULT_MAX_CHARS_OPENCLAW = 16_000  # Or 30% of context, whichever smaller
TOOL_RESULT_MAX_CHARS_LETTA = 30_000     # Bash/subagent results
TOOL_RESULT_MAX_CHARS_GREP = 10_000      # Grep specifically

TOOL_AGGREGATE_MAX_CHARS = 200_000       # Per-message aggregate (Claude Code 2026)
TOOL_INDIVIDUAL_MAX_CHARS = 50_000       # Per-tool cap

# When tool results exceed budget:
#   Claude Code: Persist to disk, replace with 2KB preview (per-tool 50K, total 200K per message)
#   OpenClaw: Soft-trim then hard-clear on 5-minute TTL
#   Pi: Hard-truncation at limit
#   Letta: Head+tail truncation, configurable via env vars
#
# Oversized result handling:
OVERSIZED_TOOL_STRATEGY = "persist_to_disk"  # Options: truncate, persist_to_disk, head_tail

OVERSIZED_TOOL_TEMPLATE = (
    "[Output truncated. Full output saved to: {overflow_path}. "
    "Use read() to view specific sections. "
    "Original size: {original_size} chars, shown: {shown_size} chars.]"
)


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 3: SESSION COMPACTION
# ═══════════════════════════════════════════════════════════════

COMPACTION_STRATEGY = "multi_tier"  # microcompact → auto-compact → manual-compact

# Trigger thresholds (convergent: fire at 80-90% capacity, NOT 95%):
COMPACTION_THRESHOLD_PCT = 0.85          # Fire auto-compaction at 85% capacity
COMPACTION_RESERVE_TOKENS = 13_000       # Keep this much headroom (Claude Code derivative)
COMPACTION_KEEP_RECENT_TOKENS = 20_000   # Always keep last N tokens of conversation

# OpenClaw: history exceeds 50% of window → starts dropping oldest chunk
OPENCLAW_HISTORY_MAX_SHARE = 0.5

# Codex: effective_window = context_window - min(max_output, 20000)
#         threshold = effective_window - 13000
#         For 200K context → fires at ~167K tokens

# Microcompaction: offload bulky tool results BEFORE context pressure
MICROCOMPACTION_ENABLED = True
MICROCOMPACTION_TOOL_RESULT_LIMIT_KB = 10  # Offload results > 10KB

# Compaction summary format (all converge on LLM → synthetic user → prepend):
COMPACTION_SUMMARY_SECTIONS = [
    "primary_request",
    "key_technical_concepts",
    "files_and_code_sections",
    "commands_and_operations",
    "errors_and_fixes",
    "problem_solving_approach",
    "all_user_messages",
    "pending_work",
    "current_work",
    "optional_next_step",
]

COMPACTION_SCRATCHPAD_ENABLED = True  # Let model reason before summarizing

# Pre-compaction flush (OpenClaw pattern): agent persists state to memory files
PRE_COMPACTION_FLUSH_ENABLED = True

# Post-compaction re-read (Claude Code pattern): re-read up to 5 recent files
POST_COMPACTION_REREAD_ENABLED = True
POST_COMPACTION_REREAD_FILE_COUNT = 5
POST_COMPACTION_REREAD_TOKEN_BUDGET = 50_000  # 10K per file

# Never break tool-call/result pairs during compaction:
COMPACTION_BOUNDARY_SAFETY = True  # Walk boundaries to keep tool-call/tool-result together


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 4: SUB-AGENT CONTEXT ISOLATION
# ═══════════════════════════════════════════════════════════════

SUBAGENT_ISOLATED_BY_DEFAULT = True
SUBAGENT_FORK_CONTEXT_SENSITIVE = True  # Only for work needing parent transcript

# Nesting depth:
MAX_SPAWN_DEPTH = 2           # OpenClaw allows 1-5, 2 is recommended
MAX_CHILDREN_PER_AGENT = 5    # OpenClaw default

# Tool policy by depth:
#   Depth 0 (main): All tools
#   Depth 1 (orchestrator, if maxDepth>=2): +sessions_spawn, subagents, sessions_list, sessions_history
#   Depth 1 (leaf, if maxDepth==1): No session tools
#   Depth 2 (leaf): No session tools, cannot spawn further
SUBAGENT_TOOL_RESTRICTION = "profile_based"  # Options: profile_based, allowlist_only

# What sub-agents inherit (convergent across all four):
SUBAGENT_BOOTSTRAP_FILES = ["AGENTS.md", "TOOLS.md"]  # NOT SOUL.md, USER.md, MEMORY.md
SUBAGENT_EXCLUDED_FILES = ["SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md", "HEARTBEAP.md", "BOOTSTRAP.md"]

# Model selection (convergence: use cheaper model for subagents):
SUBAGENT_DELEGATION_MODE = "suggest"  # "suggest" (default) or "prefer"
SUBAGENT_MODEL_OVERRIDE = "cheaper"   # Explicit cheaper model for subagent delegation

# Completion announce format (OpenClaw pattern):
ANNOUNCE_CONTEXT_FIELDS = [
    "result",          # Latest visible assistant text from child
    "status",          # completed / failed / timed_out / unknown
    "stats",           # runtime, token usage, estimated cost
    "review_instruction",  # Tell parent to verify before using result
    "follow_up",       # Continue task or record follow-up
    "session_key",     # For sessions_history retrieval
    "transcript_path", # On-disk path for full transcript
]

# Thread-bound sessions (OpenClaw on Discord):
THREAD_BOUND_SUBAGENT_ENABLED = True
THREAD_BOUND_IDLE_HOURS = 24
THREAD_BOUND_MAX_AGE_HOURS = 168  # 7 days


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 5: MEMORY PERSISTENCE (Letta MemFS pattern)
# ═══════════════════════════════════════════════════════════════

# Memory lives as markdown files in a git-backed filesystem
# Files in system/ are always in context (pinned to system prompt)
# Files outside system/ are visible by name/description, loaded on demand

MEMFS_ENABLED = True
MEMFS_SYSTEM_DIR = "system"            # Always-in-context files
MEMORY_FILE_EXTENSION = ".md"
MEMORY_FRONTMATTER_FIELDS = ["description", "limit"]

# Persistent context files (survive compaction, re-read every turn):
PERSISTENT_CONTEXT_FILES = [
    "AGENTS.md",     # Project instructions, re-read every turn    (pinned)
    "CLAUDE.md",     # User preferences, coding standards           (pinned)
    "TOOLS.md",      # Tool usage guidance                         (pinned)
]

# Memory budget tracking:
MEMORY_TOKEN_BUDGET_PCT = 0.15  # Max 15% of context for memory files
MEMORY_DEFRAG_ENABLED = True     # Periodic consolidation subagent

# Defragmentation triggers:
DEFRAG_TRIGGER = "step_count"    # Options: step_count, manual, scheduled
DEFRAG_STEP_THRESHOLD = 25       # Every 25 user messages


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 6: PRE-COMPACTION STATE FLUSH
# ═══════════════════════════════════════════════════════════════

# Before compaction destroys history, agent writes critical state to memory:
PRE_FLUSH_CHECKLIST = [
    "current_task_description",
    "files_being_edited",
    "decisions_made",
    "errors_encountered",
    "pending_todos",
    "key_discoveries",
]

PRE_FLUSH_DESTINATION = "memory.md"  # Written to MemFS or memory directory


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 7: CONTEXT ESTIMATION
# ═══════════════════════════════════════════════════════════════

# All four harnesses estimate token pressure and detect before hitting limit

CONTEXT_ESTIMATE_METHOD = "chars_per_token"  # 4 chars per token heuristic
CONTEXT_ESTIMATE_CHARS_PER_TOKEN = 4
CONTEXT_SAFETY_MARGIN = 0.10  # Keep 10% safety margin

# Token budget allocation:
BUDGET_SYSTEM_PROMPT = 0.20      # 20% for system prompt
BUDGET_MEMORY_FILES = 0.15       # 15% for memory/context files
BUDGET_CONVERSATION = 0.45       # 45% for conversation history
BUDGET_TOOL_RESULTS = 0.15       # 15% for tool results
BUDGET_OUTPUT_HEADROOM = 0.05    # 5% reserved for model output


# ═══════════════════════════════════════════════════════════════
# CONVERGENT PATTERN 8: PRACTICAL BEST PRACTICES
# ═══════════════════════════════════════════════════════════════

# From production use across all four harnesses:
BEST_PRACTICES = {
    "compact_at_task_boundaries": True,     # Don't wait for 95% capacity
    "compact_at_60_pct": True,              # Pro-active compaction at 60%
    "frontload_persistent_context": True,   # Put everything persistent in AGENTS.md/CLAUDE.md
    "lower_threshold_for_large_context": True,  # 80-85% for monorepos
    "use_subagents_for_isolated_work": True,    # Better than compaction for parallel work
    "monitor_token_burn_rate": True,            # Check /status mid-session
    "deduplicate_tool_calls": True,             # Don't re-read unchanged files at same range
    "protect_safety_rules": True,               # Never rely on compaction for rules
    "pre_compaction_state_flush": True,         # Save state before history disappears
    "post_compaction_file_reread": True,        # Re-read recent files after compaction
}


# ═══════════════════════════════════════════════════════════════
# SUMMARY: CONVERGENCE TABLE
# ═══════════════════════════════════════════════════════════════
#
# Pattern                  | Pi      | OClaw   | Claude  | Letta
# -------------------------|---------|---------|---------|------
# File read cap (lines)    | 2000    | 2000    | 2000    | 2000
# File read cap (KB)       | 50      | 50      | 256     | 10MB
# offset/limit pagination  | Yes     | Yes     | Yes     | Yes
# Continuation nudge       | Yes     | Yes     | Yes     | Yes
# Tool result budget       | 16K     | 16K     | 16-30K  | 10-30K
# Oversized → disk         | Preview | Preview | Preview | Overflow file
# Compaction trigger       | Auto    | Auto    | Auto+Man| Auto+Man
# Compaction method        | LLM sum | LLM sum | LLM sum | LLM sum
# Summary format           | User msg| User msg| User msg| User msg
# Tool-call pair safety    | Yes     | Yes     | Yes     | Yes
| Subagent isolation       | Always  | Default | Default | Default
# Fork mode                | No      | Yes     | Yes     | Yes
# Max nesting depth        | 1       | 2-5     | 1       | 1
# Memory persistence       | Limited | Limited | Limited | Git FS
# Pre-compaction flush     | No      | Yes     | No      | Reflection agent
# Post-compact re-read     | No      | No      | 5 files | No
#
# KEY INSIGHT: All four arrived at the same answers independently.
# These are not coincidences. They are convergent solutions to the same problem.
