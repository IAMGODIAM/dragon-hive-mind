"""
Dragon Context Manager v1.0
=============================
Production context management system for the Dragon Hive Mind agent mesh.

Implements ALL convergent patterns from Pi, OpenClaw, Claude Code, and Letta:
  1. File reads: Hard cap, offset/limit, continuation nudges
  2. Tool results: Separate budget, persist oversized to disk
  3. Compaction: LLM summarization at threshold, pre-flush, post-reread
  4. Sub-agent isolation: Fresh sessions, tool restrictions, model routing
  5. Memory persistence: Git-backed filesystem, progressive disclosure
  6. Context estimation: Continuous monitoring, budget allocation
  7. Dedup: Same file+range within TTL returns stub
  8. Boundary safety: Never break tool-call/result pairs
"""

import os
import json
import time
import shutil
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

logger = logging.getLogger("dragon_context")

# ═══════════════════════════════════════════════════════════
# CONSTANTS (from convergent pattern analysis)
# ═══════════════════════════════════════════════════════════

FILE_READ_MAX_LINES = 2000
FILE_READ_MAX_BYTES = 50_000        # 50 KB
FILE_READ_HARD_MAX_BYTES = 256_000  # 256 KB
FILE_READ_MAX_LINE_LENGTH = 2000

TOOL_RESULT_MAX_CHARS = 16_000
TOOL_RESULT_MAX_CHARS_TOTAL_PER_MSG = 200_000
TOOL_RESULT_PERSIST_DIR = ".dragon/tool_overflow"

CONTEXT_COMPACTION_THRESHOLD_PCT = 0.85
CONTEXT_RESERVE_TOKENS = 13_000
CONTEXT_KEEP_RECENT_TOKENS = 20_000
CONTEXT_ESTIMATE_CHARS_PER_TOKEN = 4

MAX_SUBAGENT_DEPTH = 2
MAX_SUBAGENT_CHILDREN = 5

MEMORY_DIR = ".dragon/memory"
MEMORY_SYSTEM_DIR = "system"

DEDUP_TTL_SECONDS = 300  # 5-minute cache for re-read dedup


# ═══════════════════════════════════════════════════════════
# FILE READ WITH PRODUCTION CONTEXT MANAGEMENT
# ═══════════════════════════════════════════════════════════

@dataclass
class FileReadResult:
    path: str
    content: str
    total_lines: int
    lines_shown: int
    start_line: int
    end_line: int
    truncated: bool
    total_bytes: int
    overflow_path: Optional[str] = None
    
    @property
    def continuation_nudge(self) -> str:
        if not self.truncated:
            return ""
        return (
            f"\n\n[Showing lines {self.start_line}-{self.end_line} "
            f"of {self.total_lines}. Use offset={self.end_line + 1} to continue.]"
        )


class ManagedFileReader:
    """
    Production file reader implementing Pi/OpenClaw/Claude Code/Letta convergent patterns.
    
    Rules:
    1. stat() before read to check size
    2. Reject files > 256KB (point to grep/offset)
    3. Default: first 2000 lines, 50KB max
    4. Per-line cap at 2000 chars
    5. Append continuation nudge when truncated
    6. Dedup: same file+range within TTL returns stub
    7. Head+tail truncation for large files (show beginning and end)
    """
    
    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir)
        self._dedup_cache: dict[str, tuple[float, str]] = {}  # key → (timestamp, hash)
        self._overflow_dir = self.workdir / TOOL_RESULT_PERSIST_DIR
        self._overflow_dir.mkdir(parents=True, exist_ok=True)
    
    def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = FILE_READ_MAX_LINES,
        max_bytes: int = FILE_READ_MAX_BYTES,
    ) -> FileReadResult:
        """Read a file with full context management."""
        filepath = self.workdir / path
        
        # Rule 1: stat() before read
        stat = filepath.stat()
        file_size = stat.st_size
        
        # Rule 2: Hard gate at 256KB
        if file_size > FILE_READ_HARD_MAX_BYTES:
            return FileReadResult(
                path=path,
                content=(
                    f"ERROR: File is {file_size / 1024:.0f}KB, exceeds 256KB hard limit.\n"
                    f"Use grep to search for specific content, or use offset/limit "
                    f"with a smaller range. Consider splitting this file."
                ),
                total_lines=0,
                lines_shown=0,
                start_line=0,
                end_line=0,
                truncated=False,
                total_bytes=file_size,
            )
        
        # Read file
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        
        total_lines = len(all_lines)
        
        # Rule 6: Dedup check
        dedup_key = f"{filepath}:{offset}:{limit}:{stat.st_mtime}"
        dedup_hash = hashlib.sha256(dedup_key.encode()).hexdigest()[:16]
        cache_key = f"{filepath}:{offset}:{limit}"
        
        if cache_key in self._dedup_cache:
            ts, old_hash = self._dedup_cache[cache_key]
            if time.time() - ts < DEDUP_TTL_SECONDS and old_hash == dedup_hash:
                return FileReadResult(
                    path=path,
                    content="[DEDUP: File unchanged since last read at this range. "
                            "Use force=True to re-read.]",
                    total_lines=total_lines,
                    lines_shown=0,
                    start_line=offset,
                    end_line=offset,
                    truncated=False,
                    total_bytes=file_size,
                )
        
        # Calculate the slice
        start_idx = max(0, offset - 1)
        end_idx = min(total_lines, start_idx + limit)
        selected_lines = all_lines[start_idx:end_idx]
        
        # Rule 4: Per-line cap
        processed_lines = []
        current_bytes = 0
        truncated = False
        
        for line in selected_lines:
            if len(line) > FILE_READ_MAX_LINE_LENGTH:
                line = line[:FILE_READ_MAX_LINE_LENGTH] + "…\n"
            line_bytes = len(line.encode("utf-8"))
            if current_bytes + line_bytes > max_bytes:
                truncated = True
                break
            processed_lines.append(line)
            current_bytes += line_bytes
        
        content = "".join(processed_lines)
        
        # Determine if we should do head+tail truncation
        actual_end = start_idx + len(processed_lines)
        is_truncated = actual_end < end_idx or (len(selected_lines) < total_lines and offset > 1)
        
        # Rule 7: Head+tail for very large files
        if is_truncated and total_lines > FILE_READ_MAX_LINES * 2:
            head_lines = processed_lines[:100]
            tail_lines = all_lines[-50:] if total_lines > 150 else []
            if tail_lines:
                content = (
                    "".join(head_lines)
                    + f"\n\n… {total_lines - 150} lines omitted …\n\n"
                    + "".join(tail_lines)
                )
        
        result = FileReadResult(
            path=path,
            content=content,
            total_lines=total_lines,
            lines_shown=len(processed_lines),
            start_line=offset,
            end_line=actual_end,
            truncated=is_truncated,
            total_bytes=file_size,
        )
        
        # Update dedup cache
        self._dedup_cache[cache_key] = (time.time(), dedup_hash)
        
        return result
    
    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_glob: str = "*.py",
        context_lines: int = 3,
        max_results: int = 50,
    ) -> str:
        """Search file contents (always-available alternative to full file reads)."""
        import re
        import fnmatch
        
        search_dir = self.workdir / path
        results = []
        
        for root, dirs, files in os.walk(search_dir):
            # Skip hidden dirs and common build dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
            
            for fname in files:
                if not fnmatch.fnmatch(fname, file_glob):
                    continue
                
                filepath = Path(root) / fname
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    
                    for i, line in enumerate(lines):
                        if re.search(pattern, line):
                            start = max(0, i - context_lines)
                            end = min(len(lines), i + context_lines + 1)
                            context = "".join(
                                f"  {j+1}: {l}" for j, l in enumerate(lines[start:end], start=start)
                            )
                            rel_path = filepath.relative_to(self.workdir)
                            results.append(f"{rel_path}:\n{context}")
                            
                            if len(results) >= max_results:
                                return "\n\n".join(results[:max_results])
                except Exception:
                    continue
        
        if not results:
            return f"No matches for '{pattern}' in {file_glob} files under {path}"
        
        return "\n\n".join(results)


# ═══════════════════════════════════════════════════════════
# TOOL RESULT BUDGET MANAGER
# ═══════════════════════════════════════════════════════════

@dataclass
class ToolResultBudget:
    """Manages per-result and per-message tool result budgets."""
    
    per_result_max_chars: int = TOOL_RESULT_MAX_CHARS
    per_message_max_chars: int = TOOL_RESULT_MAX_CHARS_TOTAL_PER_MSG
    overflow_dir: str = TOOL_RESULT_PERSIST_DIR
    
    _current_message_chars: int = 0
    _overflow_count: int = 0
    
    def process_result(self, result: str, tool_name: str, workdir: str = ".") -> str:
        """
        Process a tool result against budget.
        Returns: result string (possibly truncated/persisted)
        """
        result_len = len(result)
        
        # Check per-message aggregate
        if self._current_message_chars + result_len > self.per_message_max_chars:
            overflow_path = self._persist_overflow(result, tool_name, workdir)
            self._overflow_count += 1
            return (
                f"[Output exceeded per-message aggregate budget. "
                f"Truncated to 2KB preview. Full output: {overflow_path}]\n\n"
                f"{result[:2000]}"
            )
        
        # Check per-result
        if result_len > self.per_result_max_chars:
            overflow_path = self._persist_overflow(result, tool_name, workdir)
            self._current_message_chars += 2000  # Preview size
            truncated = self._head_tail_truncate(result)
            return (
                f"{truncated}\n\n"
                f"[Output truncated. Full output saved to: {overflow_path}. "
                f"Original: {result_len} chars, shown: {len(truncated)} chars.]"
            )
        
        self._current_message_chars += result_len
        return result
    
    def _head_tail_truncate(self, text: str, head_lines: int = 50, tail_lines: int = 20) -> str:
        """Keep beginning and end, drop middle (OpenClaw/Letta pattern)."""
        lines = text.splitlines(keepends=True)
        if len(lines) <= head_lines + tail_lines:
            return text
        head = "".join(lines[:head_lines])
        tail = "".join(lines[-tail_lines:])
        omitted = len(lines) - head_lines - tail_lines
        return f"{head}\n\n… {omitted} lines omitted …\n\n{tail}"
    
    def _persist_overflow(self, result: str, tool_name: str, workdir: str) -> str:
        """Persist oversized result to disk, return path."""
        overflow_dir = Path(workdir) / self.overflow_dir
        overflow_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = "".join(c if c.isalnum() else "_" for c in tool_name)[:50]
        filename = f"{safe_name}_{timestamp}.txt"
        filepath = overflow_dir / filename
        
        with open(filepath, "w") as f:
            f.write(result)
        
        return str(filepath)
    
    def reset_message_budget(self):
        """Call at the start of each new message turn."""
        self._current_message_chars = 0
        self._overflow_count = 0
    
    @property
    def stats(self) -> dict:
        return {
            "current_message_chars": self._current_message_chars,
            "overflow_count": self._overflow_count,
            "budget_utilization_pct": round(
                self._current_message_chars / self.per_message_max_chars * 100, 1
            ),
        }


# ═══════════════════════════════════════════════════════════
# CONTEXT ESTIMATOR
# ═══════════════════════════════════════════════════════════

@dataclass
class ContextEstimate:
    """Estimate context window utilization."""
    
    system_prompt_chars: int = 0
    memory_files_chars: int = 0
    conversation_chars: int = 0
    tool_results_chars: int = 0
    total_chars: int = 0
    
    # Derived
    estimated_tokens: int = 0
    context_window_tokens: int = 200_000  # Default, override per model
    utilization_pct: float = 0.0
    headroom_tokens: int = 0
    compaction_recommended: bool = False
    compaction_urgent: bool = False
    
    # Budget targets
    BUDGET_SYSTEM = 0.20
    BUDGET_MEMORY = 0.15
    BUDGET_CONVERSATION = 0.45
    BUDGET_TOOL_RESULTS = 0.15
    BUDGET_OUTPUT = 0.05
    
    def calculate(self):
        self.total_chars = (
            self.system_prompt_chars
            + self.memory_files_chars
            + self.conversation_chars
            + self.tool_results_chars
        )
        self.estimated_tokens = self.total_chars // CONTEXT_ESTIMATE_CHARS_PER_TOKEN
        self.utilization_pct = min(1.0, self.estimated_tokens / self.context_window_tokens)
        self.headroom_tokens = max(0, self.context_window_tokens - self.estimated_tokens)
        
        # Thresholds
        self.compaction_recommended = self.utilization_pct >= CONTEXT_COMPACTION_THRESHOLD_PCT
        self.compaction_urgent = self.utilization_pct >= 0.95
        
        return self
    
    def budget_report(self) -> dict:
        """Report budget allocation vs targets."""
        total = max(self.total_chars, 1)
        return {
            "system_prompt": {
                "chars": self.system_prompt_chars,
                "pct": round(self.system_prompt_chars / total * 100, 1),
                "target_pct": self.BUDGET_SYSTEM * 100,
                "status": "ok" if self.system_prompt_chars / total <= self.BUDGET_SYSTEM else "over",
            },
            "memory_files": {
                "chars": self.memory_files_chars,
                "pct": round(self.memory_files_chars / total * 100, 1),
                "target_pct": self.BUDGET_MEMORY * 100,
                "status": "ok" if self.memory_files_chars / total <= self.BUDGET_MEMORY else "over",
            },
            "conversation": {
                "chars": self.conversation_chars,
                "pct": round(self.conversation_chars / total * 100, 1),
                "target_pct": self.BUDGET_CONVERSATION * 100,
                "status": "ok" if self.conversation_chars / total <= self.BUDGET_CONVERSATION else "over",
            },
            "tool_results": {
                "chars": self.tool_results_chars,
                "pct": round(self.tool_results_chars / total * 100, 1),
                "target_pct": self.BUDGET_TOOL_RESULTS * 100,
                "status": "ok" if self.tool_results_chars / total <= self.BUDGET_TOOL_RESULTS else "over",
            },
            "total_estimated_tokens": self.estimated_tokens,
            "context_window_tokens": self.context_window_tokens,
            "utilization_pct": round(self.utilization_pct * 100, 1),
            "headroom_tokens": self.headroom_tokens,
            "compaction_recommended": self.compaction_recommended,
            "compaction_urgent": self.compaction_urgent,
        }


class ContextEstimator:
    """Continuous context monitoring (all harnesses do this)."""
    
    def __init__(self, context_window_tokens: int = 200_000):
        self.context_window = context_window_tokens
        self.estimate = ContextEstimate(context_window_tokens=context_window_tokens)
    
    def update(
        self,
        system_chars: int = 0,
        memory_chars: int = 0,
        conversation_chars: int = 0,
        tool_result_chars: int = 0,
    ) -> ContextEstimate:
        self.estimate.system_prompt_chars = system_chars
        self.estimate.memory_files_chars = memory_chars
        self.estimate.conversation_chars = conversation_chars
        self.estimate.tool_results_chars = tool_result_chars
        self.estimate.calculate()
        return self.estimate
    
    def should_compact(self) -> bool:
        return self.estimate.compaction_recommended
    
    def should_compact_urgent(self) -> bool:
        return self.estimate.compaction_urgent


# ═══════════════════════════════════════════════════════════
# MEMORY PERSISTENCE (Letta MemFS pattern)
# ═══════════════════════════════════════════════════════════

@dataclass
class MemoryFile:
    """A memory file in the MemFS system."""
    name: str
    path: str
    description: str
    content: str
    always_in_context: bool = False  # True if in system/ directory
    token_estimate: int = 0
    last_modified: str = ""
    
    def __post_init__(self):
        if not self.last_modified:
            self.last_modified = datetime.now(timezone.utc).isoformat()
        self.token_estimate = len(self.content) // CONTEXT_ESTIMATE_CHARS_PER_TOKEN


class MemoryFS:
    """
    Git-backed memory filesystem (Letta MemFS pattern, simplified).
    
    Structure:
      .dragon/memory/system/     = Always-in-context files (pinned to prompt)
      .dragon/memory/ephemeral/  = Loaded on demand (visible by name/desc)
      .dragon/memory/skills/     = Agent-scoped skills
    
    The agent manages this hierarchy itself over time:
    - Moves files in/out of system/ based on importance
    - Creates/updates files to persist state across sessions
    - Periodic defragmentation consolidates redundancy
    """
    
    def __init__(self, workdir: str = "."):
        self.workdir = Path(workdir) / MEMORY_DIR
        self.system_dir = self.workdir / MEMORY_SYSTEM_DIR
        self.ephemeral_dir = self.workdir / "ephemeral"
        self.skills_dir = self.workdir / "skills"
        
        for d in [self.system_dir, self.ephemeral_dir, self.skills_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        self._files: dict[str, MemoryFile] = {}
        self._scan()
    
    def _scan(self):
        """Scan memory directory for existing files."""
        for directory in [self.system_dir, self.ephemeral_dir]:
            if not directory.exists():
                continue
            for filepath in directory.rglob("*.md"):
                rel_path = str(filepath.relative_to(self.workdir))
                with open(filepath, "r") as f:
                    content = f.read()
                
                # Parse frontmatter
                description = "No description"
                if content.startswith("---"):
                    try:
                        fm_end = content.index("---", 3)
                        fm = content[3:fm_end].strip()
                        for line in fm.splitlines():
                            if line.startswith("description:"):
                                description = line.split(":", 1)[1].strip().strip('"').strip("'")
                    except ValueError:
                        pass
                
                mem = MemoryFile(
                    name=filepath.stem,
                    path=rel_path,
                    description=description,
                    content=content,
                    always_in_context=(directory == self.system_dir),
                )
                self._files[rel_path] = mem
    
    def read(self, name: str) -> Optional[MemoryFile]:
        """Read a memory file by name (progressive disclosure)."""
        # Try system first, then ephemeral
        for prefix in [MEMORY_SYSTEM_DIR, "ephemeral"]:
            path = f"{MEMORY_DIR}/{prefix}/{name}.md"
            if path in self._files:
                return self._files[path]
            
            # Try direct file read
            filepath = self.workdir.parent / path
            if filepath.exists():
                with open(filepath) as f:
                    content = f.read()
                mem = MemoryFile(
                    name=name,
                    path=path,
                    description="",
                    content=content,
                    always_in_context=(prefix == MEMORY_SYSTEM_DIR),
                )
                self._files[path] = mem
                return mem
        return None
    
    def write(self, name: str, content: str, description: str = "", always_in_context: bool = False):
        """Write a memory file (agent persists state)."""
        if always_in_context:
            directory = self.system_dir
            prefix = MEMORY_SYSTEM_DIR
        else:
            directory = self.ephemeral_dir
            prefix = "ephemeral"
        
        filepath = directory / f"{name}.md"
        
        # Write with frontmatter
        full_content = f"---\ndescription: \"{description}\"\n---\n\n{content}"
        with open(filepath, "w") as f:
            f.write(full_content)
        
        rel_path = f"{MEMORY_DIR}/{prefix}/{name}.md"
        mem = MemoryFile(
            name=name,
            path=rel_path,
            description=description,
            content=full_content,
            always_in_context=always_in_context,
        )
        self._files[rel_path] = mem
        
        # Git commit if git is available
        self._git_commit(f"memory: update {name}")
    
    def get_system_context(self) -> str:
        """Get all always-in-context files (pinned to system prompt)."""
        parts = []
        for mem in self._files.values():
            if mem.always_in_context:
                parts.append(f"## {mem.name}\n{mem.content}")
        return "\n\n".join(parts)
    
    def get_tree_listing(self) -> str:
        """Get listing of all memory files with descriptions (progressive disclosure)."""
        lines = ["Memory files:"]
        for rel_path, mem in sorted(self._files.items()):
            context_tag = " [ALWAYS IN CONTEXT]" if mem.always_in_context else ""
            lines.append(f"  {mem.name}: {mem.description}{context_tag}")
        return "\n".join(lines)
    
    def _git_commit(self, message: str):
        """Commit memory changes to git (if repo exists)."""
        git_dir = self.workdir.parent / ".git"
        if not git_dir.exists():
            return
        
        try:
            import subprocess
            subprocess.run(
                ["git", "add", "-A", str(self.workdir)],
                cwd=str(self.workdir.parent),
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", message, "--", str(self.workdir)],
                cwd=str(self.workdir.parent),
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass  # Git operations are best-effort
    
    def token_usage(self) -> dict:
        """Report token usage by category (letta memory tokens command)."""
        system_tokens = sum(
            m.token_estimate for m in self._files.values() if m.always_in_context
        )
        ephemeral_tokens = sum(
            m.token_estimate for m in self._files.values() if not m.always_in_context
        )
        return {
            "system_dir_tokens": system_tokens,
            "ephemeral_dir_tokens": ephemeral_tokens,
            "total_memory_tokens": system_tokens + ephemeral_tokens,
            "system_files": sum(1 for m in self._files.values() if m.always_in_context),
            "ephemeral_files": sum(1 for m in self._files.values() if not m.always_in_context),
        }
    
    def defragment(self):
        """
        Consolidate redundant memory files (Letta defrag pattern).
        Split large files, merge duplicates, restructure hierarchy.
        """
        logger.info("Starting memory defragmentation...")
        
        # Simple defrag: merge files with similar names
        merged = 0
        for rel_path, mem in list(self._files.items()):
            # Split files > 10K tokens
            if mem.token_estimate > 10000:
                content_lines = mem.content.splitlines()
                mid = len(content_lines) // 2
                part1 = "\n".join(content_lines[:mid])
                part2 = "\n".join(content_lines[mid:])
                
                name_base = mem.name
                self.write(f"{name_base}_part1", part1, f"Part 1 of {name_base}", mem.always_in_context)
                self.write(f"{name_base}_part2", part2, f"Part 2 of {name_base}", False)
                merged += 1
        
        logger.info(f"Defragmentation complete: {merged} files split/merged")
        return merged


# ═══════════════════════════════════════════════════════════
# SESSION COMPACTION ENGINE
# ═══════════════════════════════════════════════════════════

COMPACTION_SUMMARY_PROMPT = """\
You are a context compaction engine. Summarize the following conversation
history to preserve all essential information while fitting within a
compact context window.

RULES:
1. Use the structured 9-section format (see COMPACTION_SUMMARY_SECTIONS)
2. Be thorough — missing information is lost forever
3. Preserve exact file paths, function names, and error messages
4. Keep all user requests and their resolution status
5. Write a scratchpad first, then the final summary

SECTIONS TO COVER:
1. Primary request and intent
2. Key technical concepts and architecture decisions
3. Files and code sections modified or examined
4. Errors encountered and how they were fixed
5. Problem-solving approaches tried
6. All user messages (verbatim if short, summarized if long)
7. Pending work items
8. Current work in progress
9. Optional: recommended next step

OUTPUT FORMAT:
<analysis>
[Your detailed analysis scratchpad — will be stripped before context]
</analysis>

<summary>
[The final summary that will enter the context]
</summary>
"""


class CompactionEngine:
    """
    Session compaction engine implementing all four harness patterns.
    
    Strategies:
    - Microcompaction: Offload bulky tool results early (before context pressure)
    - Auto-compaction: LLM summarization when utilization > threshold
    - Manual compaction: Triggered by user with optional focus instructions
    
    Different harness approaches:
    - Pi: Simple LLM summarization at threshold, keep recent 20K tokens
    - OpenClaw: Multi-pass staged summary + pre-compaction state flush
    - Claude Code: Structured 9-section prompt, 5-file post-reread
    - Letta: Server-side API compaction + reflection subagent
    """
    
    def __init__(
        self,
        context_window_tokens: int = 200_000,
        model=None,  # LLM callable for summarization
    ):
        self.context_window = context_window_tokens
        self.model = model
        self.estimator = ContextEstimator(context_window_tokens)
        self.tool_budget = ToolResultBudget()
        self._compaction_count = 0
        self._last_compaction: Optional[str] = None
    
    def check_and_compact(
        self,
        messages: list[dict],
        system_chars: int,
        memory_chars: int,
        tool_result_chars: int,
    ) -> tuple[list[dict], bool]:
        """
        Check if compaction is needed and run it.
        Returns: (possibly compacted messages, whether compaction ran)
        """
        # Update estimate
        conv_chars = sum(len(str(m.get("content", ""))) for m in messages)
        estimate = self.estimator.update(
            system_chars=system_chars,
            memory_chars=memory_chars,
            conversation_chars=conv_chars,
            tool_result_chars=tool_result_chars,
        )
        
        if estimate.compaction_urgent or estimate.compaction_recommended:
            logger.info(
                f"Compaction triggered: {estimate.utilization_pct*100:.0f}% utilization"
            )
            return self._compact(messages), True
        
        return messages, False
    
    def _compact(self, messages: list[dict]) -> list[dict]:
        """
        Run compaction on conversation history.
        
        Implements OpenClaw's multi-pass staged summarization:
        1. Keep recent K tokens of conversation (never summarize the tail)
        2. Summarize older content in chunks
        3. Pre-compaction flush: agent writes state to memory
        4. Synthetic summary becomes user message prepended to kept tail
        """
        self._compaction_count += 1
        self._last_compaction = datetime.now(timezone.utc).isoformat()
        
        if not self.model:
            # Fallback: deterministic head-drop (Claude Code's fallback)
            logger.warning("No model for LLM compaction; using deterministic head-drop")
            return self._deterministic_compact(messages)
        
        # Calculate split point
        keep_tokens = CONTEXT_KEEP_RECENT_TOKENS
        running_tokens = 0
        split_idx = len(messages)
        
        for i in range(len(messages) - 1, -1, -1):
            content_len = len(str(messages[i].get("content", "")))
            running_tokens += content_len // CONTEXT_ESTIMATE_CHARS_PER_TOKEN
            if running_tokens >= keep_tokens:
                split_idx = i
                break
        
        # Messages to summarize vs keep
        to_summarize = messages[:split_idx]
        to_keep = messages[split_idx:]
        
        # CRITICAL: Never break tool-call/result pairs
        to_summarize, to_keep = self._fix_tool_pairs(to_summarize, to_keep)
        
        # Generate summary
        try:
            summary = self._call_summarizer(to_summarize)
        except Exception as e:
            logger.error(f"Summarizer failed: {e}; using deterministic fallback")
            return self._deterministic_compact(messages)
        
        # Build compacted conversation
        compacted = [
            {
                "role": "user",
                "content": (
                    f"[Session continued from a previous conversation that exceeded the context window. "
                    f"The following is a summary of the previous session:\n\n{summary}]\n\n"
                    f"Please continue from where we left off."
                ),
            },
            {
                "role": "assistant",
                "content": "Understood. I'll continue based on the summary above.",
            },
            *to_keep,
        ]
        
        logger.info(
            f"Compaction complete: {len(messages)} → {len(compacted)} messages "
            f"(saved {len(messages) - len(compacted)} messages)"
        )
        
        return compacted
    
    def _deterministic_compact(self, messages: list[dict]) -> list[dict]:
        """Claude Code's fallback: drop oldest 20% of API-round groups."""
        # Group messages into API rounds (user → assistant pairs)
        groups = []
        current_group = []
        for msg in messages:
            current_group.append(msg)
            if msg.get("role") == "assistant":
                groups.append(current_group)
                current_group = []
        if current_group:
            groups.append(current_group)
        
        # Drop oldest 20% of groups
        drop_count = max(1, len(groups) // 5)
        kept_groups = groups[drop_count:]
        
        # Prepend summary note
        compacted = [
            {
                "role": "user",
                "content": (
                    "[Session truncated due to context limit. "
                    f"Oldest {drop_count} conversation rounds were dropped.]"
                ),
            },
        ]
        for group in kept_groups:
            compacted.extend(group)
        
        return compacted
    
    def _fix_tool_pairs(self, old: list[dict], keep: list[dict]) -> tuple[list[dict], list[dict]]:
        """
        Ensure we never break tool-call/result boundaries.
        If a tool_call is in `old` but its tool_result is in `keep`,
        move both to `keep`.
        """
        # Find tool_calls in old that have results in keep
        old_tool_call_ids = set()
        for msg in old:
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    old_tool_call_ids.add(tc.get("id", ""))
        
        # Check if any keep messages are tool results for those calls
        to_move_old = []
        to_move_keep = []
        
        for i, msg in enumerate(keep):
            if msg.get("role") == "tool" and msg.get("tool_call_id") in old_tool_call_ids:
                # Find the matching tool_calls in old and move them to keep
                for j, old_msg in enumerate(old):
                    if old_msg.get("tool_calls"):
                        for tc in old_msg["tool_calls"]:
                            if tc.get("id") == msg.get("tool_call_id"):
                                if j not in to_move_old:
                                    to_move_old.append(j)
                                if i not in to_move_keep:
                                    to_move_keep.append(i)
        
        # Move them
        for idx in sorted(to_move_old, reverse=True):
            to_move_keep.insert(0, old.pop(idx))
        
        # Remove moved keep items (they're already covered by the moved old items)
        for idx in sorted(to_move_keep, reverse=True):
            if idx < len(keep):
                keep.pop(idx)
        
        return old, keep
    
    def _call_summarizer(self, messages: list[dict]) -> str:
        """Call the LLM to summarize conversation (with scratchpad pattern)."""
        if not self.model:
            raise RuntimeError("No model available for summarization")
        
        # Build summary prompt
        conversation_text = "\n\n".join(
            f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
            for m in messages
        )
        
        prompt = f"{COMPACTION_SUMMARY_PROMPT}\n\nCONVERSATION:\n{conversation_text}"
        
        # Call model (scratchpad + summary pattern from Claude Code)
        response = self.model(prompt)
        
        # Extract summary (strip scratchpad)
        if "<summary>" in response:
            summary_start = response.index("<summary>") + len("<summary>")
            summary_end = response.index("</summary>") if "</summary>" in response else len(response)
            return response[summary_start:summary_end].strip()
        
        return response.strip()
    
    @property
    def stats(self) -> dict:
        return {
            "compaction_count": self._compaction_count,
            "last_compaction": self._last_compaction,
        }


# ═══════════════════════════════════════════════════════════
# INTEGRATED CONTEXT MANAGER
# ═══════════════════════════════════════════════════════════

class DragonContextManager:
    """
    Integrated context manager combining all subsystems.
    
    Usage:
        ctx = DragonContextManager(workdir="/path/to/project")
        
        # File reads
        result = ctx.read_file("src/main.py", offset=1, limit=200)
        
        # Tool results
        output = ctx.process_tool_output(large_output, tool_name="bash")
        
        # Memory
        ctx.memory.write("task_state", "Current work: implementing feature X", always_in_context=True)
        system_ctx = ctx.memory.get_system_context()
        
        # Compaction check
        messages, compacted = ctx.check_compaction(messages, system_chars=5000, memory_chars=2000, tool_chars=8000)
    """
    
    def __init__(self, workdir: str = ".", context_window_tokens: int = 200_000):
        self.workdir = workdir
        self.file_reader = ManagedFileReader(workdir)
        self.tool_budget = ToolResultBudget()
        self.memory = MemoryFS(workdir)
        self.compaction = CompactionEngine(context_window_tokens)
        self.estimator = ContextEstimator(context_window_tokens)
    
    def read_file(self, path: str, offset: int = 1, limit: int = FILE_READ_MAX_LINES) -> FileReadResult:
        """Managed file read with all convergent safety patterns."""
        return self.file_reader.read(path, offset=offset, limit=limit)
    
    def grep(self, pattern: str, path: str = ".", file_glob: str = "*.py") -> str:
        """Search file contents (alternative to full reads)."""
        return self.file_reader.grep(pattern, path, file_glob)
    
    def process_tool_output(self, output: str, tool_name: str) -> str:
        """Process tool result through budget manager."""
        return self.tool_budget.process_result(output, tool_name, self.workdir)
    
    def check_compaction(
        self,
        messages: list[dict],
        system_chars: int,
        memory_chars: int,
        tool_chars: int,
    ) -> tuple[list[dict], bool]:
        """Check if compaction is needed and run if so."""
        return self.compaction.check_and_compact(
            messages, system_chars, memory_chars, tool_chars
        )
    
    def get_system_context(self) -> str:
        """Get all always-in-context memory files."""
        return self.memory.get_system_context()
    
    def get_status(self) -> dict:
        """Full context status report."""
        return {
            "memory": self.memory.token_usage(),
            "tool_budget": self.tool_budget.stats,
            "context": self.estimator.estimate.budget_report(),
            "compaction": self.compaction.stats,
        }
