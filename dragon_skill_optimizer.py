"""
DRAGON SKILL OPTIMIZATION ENGINE v1.0
======================================
Synthesized from:
  - SkillOpt (Microsoft, arxiv 2605.23904): Text-space optimizer for agent skills
  - book-to-skill: Compile-time knowledge extraction from documents
  - Context Management patterns from Pi/OpenClaw/Claude Code/Letta

This is the system that makes ITSELF smarter over time.

Key insight from SkillOpt: Skills should be TRAINED like neural networks —
with epochs, batch sizes, learning rates, and validation gates — but in
TEXT space, not weight space. The skill document is the external state
being optimized. The model stays frozen.

Key insight from book-to-skill: Knowledge from books/documents should be
extracted ONCE at compile time into structured skills, not dumped as raw
text at query time. Structure > density > completeness.

Combined: A self-improving agent that:
  1. Extracts knowledge from documents into structured skills
  2. Trains those skills using execution feedback (SkillOpt loop)
  3. Validates improvements on held-out data
  4. Persists validated improvements to git-backed memory
  5. Uses progressive disclosure to manage context budget
"""

import os
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

logger = logging.getLogger("dragon_skill_opt")

# ═══════════════════════════════════════════════════════════
# SKILL DOCUMENT — The trainable artifact
# ═══════════════════════════════════════════════════════════

@dataclass
class SkillVersion:
    """A single version of a skill document."""
    skill_name: str
    content: str
    version: int
    timestamp: str
    validation_score: float = 0.0
    is_best: bool = False
    edit_summary: str = ""  # What changed vs previous version
    
    @property
    def token_estimate(self) -> int:
        return len(self.content) // 4  # 4 chars per token heuristic


# ═══════════════════════════════════════════════════════════
# SKILLOPT: TRAINABLE SKILL OPTIMIZER
# ═══════════════════════════════════════════════════════════
# From: arxiv.org/abs/2605.23904
# "SkillOpt: Executive Strategy for Self-Evolving Agent Skills"
#
# Core loop:
#   1. Sample rollout batch from training data
#   2. Execute target model with current skill → scored trajectories
#   3. Separate failures from successes, partition into reflection minibatches
#   4. Optimizer model proposes add/delete/replace edits
#   5. Merge and rank edits, clip to learning-rate budget
#   6. Apply bounded edit → candidate skill
#   7. Validate on held-out split
#   8. Accept only if score strictly improves
#   9. Rejected edits → negative feedback buffer
#  10. Epoch-wise slow/meta update captures long-horizon patterns
# ═══════════════════════════════════════════════════════════

@dataclass
class SkillOptConfig:
    """Configuration for the SkillOpt training loop."""
    num_epochs: int = 4
    rollout_batch_size: int = 40
    reflection_minibatch_size: int = 8
    learning_rate_edits: int = 4  # Max edits per step (textual learning rate)
    learning_rate_schedule: str = "cosine"  # constant, linear, cosine, autonomous
    validation_strict: bool = True  # Only accept if score strictly improves
    use_rejected_buffer: bool = True
    use_slow_meta_update: bool = True
    slow_update_samples: int = 20  # Tasks per epoch for slow update
    max_skill_tokens: int = 2000   # Maximum deployed skill size
    

@dataclass
class RolloutResult:
    """Result of a single task execution (trajectory)."""
    task_id: str
    success: bool
    score: float
    trajectory: list[dict] = field(default_factory=list)
    error: str = ""
    output: str = ""


@dataclass
class SkillEdit:
    """A proposed edit to the skill document."""
    edit_type: str  # "add", "delete", "replace"
    target: str  # Section or text to edit
    content: str  # New content
    reason: str  # Why this edit was proposed
    source: str  # "failure" or "success" reflection
    expected_utility: float = 0.5  # Optimizer's confidence


@dataclass
class OptimizerState:
    """State maintained across SkillOpt training loop."""
    current_skill: str = ""
    best_skill: str = ""
    best_validation_score: float = 0.0
    current_score: float = 0.0
    rejected_edits: list[SkillEdit] = field(default_factory=list)
    accepted_edits: list[SkillEdit] = field(default_factory=list)
    skill_history: list[SkillVersion] = field(default_factory=list)
    epoch: int = 0
    step: int = 0
    
    # Slow/meta update state
    slow_update_field: str = ""  # Protected region for long-horizon patterns
    meta_skill_guidance: str = ""  # Optimizer-side only, never deployed


class SkillOptimizer:
    """
    SkillOpt: Text-space optimizer for agent skills.
    
    Trains a skill document using execution feedback, with deep-learning-style
    controls (epochs, batch sizes, learning rates, validation gates) but in
    TEXT space — the model weights stay frozen.
    
    Usage:
        optimizer = SkillOptimizer(
            optimizer_model=my_llm_callable,
            task_executor=my_executor,
        )
        
        result = optimizer.train(
            skill_name="search_strategy",
            initial_skill="Always cite your sources.",
            train_tasks=train_data,
            val_tasks=val_data,
            config=SkillOptConfig(),
        )
        
        best_skill = result.best_skill  # Deploy this
    """
    
    def __init__(
        self,
        optimizer_model: Callable,  # LLM callable for reflection/editing
        task_executor: Callable,    # Execute a task, return RolloutResult
    ):
        self.optimizer = optimizer_model
        self.executor = task_executor
    
    def train(
        self,
        skill_name: str,
        initial_skill: str,
        train_tasks: list[dict],
        val_tasks: list[dict],
        config: SkillOptConfig = None,
    ) -> OptimizerState:
        """
        Full SkillOpt training loop.
        
        This is the core algorithm from the paper:
        1. Rollout batch → scored trajectories
        2. Reflection minibatches → proposed edits
        3. Bounded update (learning rate = max edits per step)
        4. Validation gate (only accept strict improvements)
        5. Rejected edit buffer (negative feedback)
        6. Epoch-wise slow/meta update
        """
        config = config or SkillOptConfig()
        state = OptimizerState(current_skill=initial_skill, best_skill=initial_skill)
        
        logger.info(
            f"SkillOpt training '{skill_name}': "
            f"{len(train_tasks)} train, {len(val_tasks)} val, "
            f"{config.num_epochs} epochs"
        )
        
        for epoch in range(config.num_epochs):
            state.epoch = epoch
            logger.info(f"Epoch {epoch + 1}/{config.num_epochs}")
            
            # Phase 1: Rollout batch
            batch = train_tasks[:config.rollout_batch_size]
            
            # Phase 2: Execute trajectories
            rollouts = [self._execute_task(t, state.current_skill) for t in batch]
            
            # Phase 3: Separate failures and successes
            failures = [r for r in rollouts if not r.success]
            successes = [r for r in rollouts if r.success]
            
            if not failures:
                logger.info(f"No failures in epoch {epoch + 1}; skill is working well")
                break
            
            # Phase 4: Reflection minibatches → proposed edits
            proposed_edits = self._reflect_and_propose(
                failures, successes, state, config
            )
            
            # Phase 5: Merge, rank, clip to learning-rate budget
            selected_edits = self._select_edits(proposed_edits, config.learning_rate_edits)
            
            # Phase 6: Apply bounded edit → candidate skill
            candidate_skill = self._apply_edits(state.current_skill, selected_edits)
            
            # Phase 7: Validation gate
            val_score = self._validate(candidate_skill, val_tasks)
            
            if val_score > state.current_score:
                # Accept!
                state.current_skill = candidate_skill
                state.current_score = val_score
                state.accepted_edits.extend(selected_edits)
                
                if val_score > state.best_validation_score:
                    state.best_skill = candidate_skill
                    state.best_validation_score = val_score
                    logger.info(f"  New best: {val_score:.2f}")
            else:
                # Reject → negative feedback buffer
                state.rejected_edits.extend(selected_edits)
                logger.debug(f"  Rejected: {val_score:.2f} ≤ {state.current_score:.2f}")
            
            # Phase 8: Epoch-wise slow/meta update
            if config.use_slow_meta_update:
                self._slow_update(state, train_tasks, config)
            
            # Record version
            state.skill_history.append(SkillVersion(
                skill_name=skill_name,
                content=state.current_skill,
                version=len(state.skill_history),
                timestamp=datetime.now(timezone.utc).isoformat(),
                validation_score=val_score,
                is_best=(val_score >= state.best_validation_score),
                edit_summary=f"Epoch {epoch + 1}: {len(selected_edits)} edits",
            ))
        
        logger.info(
            f"Training complete. Best score: {state.best_validation_score:.2f}. "
            f"Accepted {len(state.accepted_edits)} edits, "
            f"rejected {len(state.rejected_edits)}."
        )
        
        return state
    
    def _execute_task(self, task: dict, skill: str) -> RolloutResult:
        """Execute a single task with the current skill."""
        try:
            return self.executor(task, skill)
        except Exception as e:
            return RolloutResult(
                task_id=task.get("id", "?"),
                success=False,
                score=0.0,
                error=str(e)[:200],
            )
    
    def _reflect_and_propose(
        self,
        failures: list[RolloutResult],
        successes: list[RolloutResult],
        state: OptimizerState,
        config: SkillOptConfig,
    ) -> list[SkillEdit]:
        """
        Phase 4: Reflection minibatches → proposed edits.
        
        Key insight from SkillOpt: Don't reflect on single trajectories
        (anecdotal). Use minibatches to expose RECURRING procedural errors.
        """
        edits = []
        
        # Reflect on failures (what's going wrong?)
        if failures:
            failure_text = "\n\n".join(
                f"Task {r.task_id}: {r.error}\nOutput: {r.output[:500]}"
                for r in failures[:config.reflection_minibatch_size]
            )
            
            reflection_prompt = f"""\
You are a skill optimizer. Analyze these FAILURES and propose edits to the skill.

CURRENT SKILL:
{state.current_skill}

FAILURES:
{failure_text}

REJECTED EDITS (don't repeat these):
{self._format_rejected(state.rejected_edits[:10])}

Propose 3-5 specific edits as add/delete/replace operations.
Each edit should fix a RECURRING pattern, not a single instance.

Format:
EDIT: add|delete|replace
TARGET: <section or text>
CONTENT: <new content>
REASON: <why this helps>
UTILITY: <0-1 confidence>
"""
            
            response = self.optimizer(reflection_prompt)
            edits.extend(self._parse_edits(response, source="failure"))
        
        # Reflect on successes (what's working and should be preserved?)
        if successes:
            success_text = "\n\n".join(
                f"Task {r.task_id}: {r.output[:300]}"
                for r in successes[:4]
            )
            
            preserve_prompt = f"""\
You are a skill optimizer. These tasks SUCCEEDED.
Identify what the skill is doing RIGHT that should be preserved.

CURRENT SKILL:
{state.current_skill}

SUCCESSES:
{success_text}

Propose 1-2 edits that PRESERVE successful behaviors while removing
any conflicting instructions.

Format:
EDIT: add|replace
TARGET: <section>
CONTENT: <content>
REASON: <why this preserves success>
UTILITY: <0-1>
"""
            
            response = self.optimizer(preserve_prompt)
            edits.extend(self._parse_edits(response, source="success"))
        
        return edits
    
    def _select_edits(
        self,
        proposed: list[SkillEdit],
        budget: int,
    ) -> list[SkillEdit]:
        """Phase 5: Merge, rank by expected utility, clip to budget."""
        # Deduplicate
        seen = set()
        unique = []
        for e in proposed:
            key = (e.edit_type, e.target, e.content[:100])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        
        # Rank by utility, prefer failure corrections
        unique.sort(key=lambda e: e.expected_utility + (0.2 if e.source == "failure" else 0), reverse=True)
        
        return unique[:budget]
    
    def _apply_edits(self, skill: str, edits: list[SkillEdit]) -> str:
        """Phase 6: Apply bounded edits to create candidate skill."""
        result = skill
        
        for edit in edits:
            if edit.edit_type == "add":
                result = result + f"\n\n{edit.content}"
            elif edit.edit_type == "replace":
                if edit.target in result:
                    result = result.replace(edit.target, edit.content, 1)
                else:
                    result = result + f"\n\n{edit.content}"
            elif edit.edit_type == "delete":
                if edit.target in result:
                    result = result.replace(edit.target, "")
        
        # Enforce max size
        if len(result) // 4 > 2000:  # 2000 token max
            result = result[:8000]  # ~2000 tokens at 4 chars/token
        
        return result.strip()
    
    def _validate(self, skill: str, val_tasks: list[dict]) -> float:
        """Phase 7: Evaluate candidate skill on held-out validation set."""
        if not val_tasks:
            return 0.0
        
        scores = []
        for task in val_tasks[:20]:  # Validate on subset for speed
            result = self._execute_task(task, skill)
            scores.append(result.score)
        
        return sum(scores) / len(scores) if scores else 0.0
    
    def _slow_update(
        self,
        state: OptimizerState,
        train_tasks: list[dict],
        config: SkillOptConfig,
    ):
        """
        Phase 8: Epoch-wise slow/meta update.
        
        Compares previous epoch skill vs current skill on same tasks.
        Captures LONG-HORIZON patterns that single-step edits miss.
        Writes to protected slow-update field.
        """
        if len(state.skill_history) < 2:
            return
        
        prev_skill = state.skill_history[-2].content
        curr_skill = state.current_skill
        
        # Sample tasks and compare
        sample = train_tasks[:config.slow_update_samples]
        
        prev_scores = []
        curr_scores = []
        for task in sample:
            prev_result = self._execute_task(task, prev_skill)
            curr_result = self._execute_task(task, curr_skill)
            prev_scores.append(prev_result.score)
            curr_scores.append(curr_result.score)
        
        avg_prev = sum(prev_scores) / len(prev_scores) if prev_scores else 0
        avg_curr = sum(curr_scores) / len(curr_scores) if curr_scores else 0
        
        improvements = sum(1 for p, c in zip(prev_scores, curr_scores) if c > p)
        regressions = sum(1 for p, c in zip(prev_scores, curr_scores) if c < p)
        
        # Write slow update guidance
        state.slow_update_field = (
            f"[Slow Update Epoch {state.epoch + 1}]\n"
            f"Improvements: {improvements}/{len(sample)}\n"
            f"Regressions: {regressions}/{len(sample)}\n"
            f"Score: {avg_prev:.2f} → {avg_curr:.2f}\n"
        )
        
        # Meta guidance (optimizer-side only, never deployed)
        state.meta_skill_guidance = (
            f"Accepted edits this epoch: {len(state.accepted_edits)}\n"
            f"Rejected edits: {len(state.rejected_edits)}\n"
            f"Net improvement: {avg_curr - avg_prev:+.2f}\n"
        )
    
    def _format_rejected(self, edits: list[SkillEdit]) -> str:
        if not edits:
            return "None"
        return "\n".join(f"- {e.edit_type} '{e.target[:50]}': {e.reason[:100]}" for e in edits)
    
    def _parse_edits(self, response: str, source: str) -> list[SkillEdit]:
        """Parse structured edits from optimizer response."""
        edits = []
        current = {}
        
        for line in response.splitlines():
            line = line.strip()
            if line.startswith("EDIT:"):
                if current.get("edit_type"):
                    edits.append(SkillEdit(
                        edit_type=current.get("edit_type", "add"),
                        target=current.get("target", ""),
                        content=current.get("content", ""),
                        reason=current.get("reason", ""),
                        source=source,
                        expected_utility=float(current.get("utility", "0.5")),
                    ))
                current = {"edit_type": line.split(":", 1)[1].strip()}
            elif line.startswith("TARGET:"):
                current["target"] = line.split(":", 1)[1].strip()
            elif line.startswith("CONTENT:"):
                current["content"] = line.split(":", 1)[1].strip()
            elif line.startswith("REASON:"):
                current["reason"] = line.split(":", 1)[1].strip()
            elif line.startswith("UTILITY:"):
                current["utility"] = line.split(":", 1)[1].strip()
        
        # Don't forget the last one
        if current.get("edit_type"):
            edits.append(SkillEdit(
                edit_type=current.get("edit_type", "add"),
                target=current.get("target", ""),
                content=current.get("content", ""),
                reason=current.get("reason", ""),
                source=source,
                expected_utility=float(current.get("utility", "0.5")),
            ))
        
        return edits


# ═══════════════════════════════════════════════════════════
# BOOK-TO-SKILL: COMPILE-TIME KNOWLEDGE EXTRACTION
# ═══════════════════════════════════════════════════════════
# From: github.com/virgiliojr94/book-to-skill
#
# Key insight: Knowledge from documents should be extracted ONCE at compile
# time into structured skills, not dumped as raw text at query time.
#
# Output structure:
#   SKILL.md          — Core mental models + chapter index (~4K tokens)
#   chapters/*.md     — Per-chapter summaries (~1K tokens each, on-demand)
#   glossary.md       — Key terms with chapter refs (~1.5K tokens)
#   patterns.md       — Techniques, algorithms, design patterns (~2K tokens)
#   cheatsheet.md     — Decision tables, quick-reference (~1K tokens)
#
# Design principles:
#   1. Density over completeness — 1K summary > 10K excerpt
#   2. Practitioner voice — "Use X when Y", not "The book explains X"
#   3. Front-loaded SKILL.md — compaction keeps first ~5K tokens
#   4. On-demand chapters — topic index tells which file to read
#   5. Never raw text — always synthesize, extract signal
# ═══════════════════════════════════════════════════════════

@dataclass
class ExtractedSkill:
    """A skill extracted from a document."""
    name: str
    slug: str
    source_document: str
    skill_md: str
    chapters: dict[str, str] = field(default_factory=dict)
    glossary: str = ""
    patterns: str = ""
    cheatsheet: str = ""
    total_tokens: int = 0
    
    def save(self, base_dir: str = "~/.dragon/skills"):
        """Save extracted skill to disk."""
        skill_dir = Path(base_dir) / self.slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        
        # SKILL.md (front-loaded, always in context)
        with open(skill_dir / "SKILL.md", "w") as f:
            f.write(self.skill_md)
        
        # Chapters (on-demand)
        chapters_dir = skill_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        for name, content in self.chapters.items():
            with open(chapters_dir / f"{name}.md", "w") as f:
                f.write(content)
        
        # Reference files
        if self.glossary:
            with open(skill_dir / "glossary.md", "w") as f:
                f.write(self.glossary)
        
        if self.patterns:
            with open(skill_dir / "patterns.md", "w") as f:
                f.write(self.patterns)
        
        if self.cheatsheet:
            with open(skill_dir / "cheatsheet.md", "w") as f:
                f.write(self.cheatsheet)
        
        # Metadata
        with open(skill_dir / "metadata.json", "w") as f:
            json.dump({
                "name": self.name,
                "slug": self.slug,
                "source": self.source_document,
                "total_tokens": self.total_tokens,
                "num_chapters": len(self.chapters),
                "created": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
        
        logger.info(f"Saved skill '{self.slug}' to {skill_dir}")
        return skill_dir


class BookToSkillExtractor:
    """
    Extract structured skills from documents (books, papers, docs).
    
    This is the compile-time knowledge extraction pipeline:
    1. Extract text from document (PDF, EPUB, etc.)
    2. Analyze structure (title, author, chapters, ToC)
    3. Generate per-chapter summaries (800-1200 tokens each)
    4. Extract glossary, patterns, cheatsheet
    5. Generate master SKILL.md with core mental models
    6. Save to skill directory for on-demand loading
    """
    
    def __init__(self, llm: Callable):
        self.llm = llm
    
    def extract(
        self,
        document_path: str,
        skill_name: str = None,
        is_technical: bool = True,
    ) -> ExtractedSkill:
        """
        Full extraction pipeline.
        
        Args:
            document_path: Path to document (PDF, EPUB, etc.)
            skill_name: Name for the skill (derived from filename if not given)
            is_technical: Whether this is a technical book (uses better extraction)
        """
        path = Path(document_path)
        slug = skill_name or path.stem.lower().replace(" ", "-")
        
        logger.info(f"Extracting skill from: {path}")
        
        # Phase 1: Extract text
        raw_text = self._extract_text(path, is_technical)
        
        # Phase 2: Analyze structure
        structure = self._analyze_structure(raw_text)
        
        # Phase 3: Generate per-chapter summaries
        chapters = self._summarize_chapters(raw_text, structure)
        
        # Phase 4: Extract reference materials
        glossary = self._extract_glossary(raw_text, structure)
        patterns = self._extract_patterns(raw_text, structure)
        cheatsheet = self._extract_cheatsheet(raw_text, structure)
        
        # Phase 5: Generate master SKILL.md
        skill_md = self._generate_skill_md(
            structure, chapters, glossary, patterns, cheatsheet
        )
        
        total_tokens = (
            len(skill_md) // 4
            + sum(len(c) // 4 for c in chapters.values())
            + len(glossary) // 4
            + len(patterns) // 4
            + len(cheatsheet) // 4
        )
        
        return ExtractedSkill(
            name=skill_name or structure.get("title", slug),
            slug=slug,
            source_document=str(path),
            skill_md=skill_md,
            chapters=chapters,
            glossary=glossary,
            patterns=patterns,
            cheatsheet=cheatsheet,
            total_tokens=total_tokens,
        )
    
    def _extract_text(self, path: Path, is_technical: bool) -> str:
        """Extract text from document."""
        suffix = path.suffix.lower()
        
        if suffix == ".pdf":
            return self._extract_pdf(path, is_technical)
        elif suffix == ".epub":
            return self._extract_epub(path)
        elif suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")
        else:
            return path.read_text(encoding="utf-8", errors="replace")
    
    def _extract_pdf(self, path: Path, is_technical: bool) -> str:
        """Extract text from PDF."""
        try:
            if is_technical:
                # Use docling for technical books (preserves tables, code)
                try:
                    from docling.document_converter import DocumentConverter
                    converter = DocumentConverter()
                    result = converter.convert(str(path))
                    return result.document.export_to_markdown()
                except ImportError:
                    pass
            
            # Fallback: PyPDF2
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(str(path))
                return "\n\n".join(page.extract_text() or "" for page in reader.pages)
            except ImportError:
                pass
            
            # Last resort: pdfminer
            try:
                from pdfminer.high_level import extract_text
                return extract_text(str(path))
            except ImportError:
                pass
            
            return f"[PDF extraction requires: pip install docling PyPDF2 pdfminer.six]"
        except Exception as e:
            return f"[PDF extraction failed: {e}]"
    
    def _extract_epub(self, path: Path) -> str:
        """Extract text from EPUB."""
        try:
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
            
            book = epub.read_epub(str(path))
            texts = []
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                soup = BeautifulSoup(item.get_content(), "html.parser")
                texts.append(soup.get_text())
            return "\n\n".join(texts)
        except ImportError:
            return "[EPUB extraction requires: pip install ebooklib beautifulsoup4]"
    
    def _analyze_structure(self, text: str) -> dict:
        """Analyze document structure (title, author, chapters)."""
        # Use first 5K chars for structure analysis
        sample = text[:5000]
        
        prompt = f"""\
Analyze this document and extract its structure.

DOCUMENT (first 5000 chars):
{sample}

Return JSON:
{{
  "title": "Document title",
  "author": "Author name",
  "num_chapters": N,
  "chapter_titles": ["Chapter 1: ...", "Chapter 2: ..."],
  "has_glossary": true/false,
  "has_index": true/false,
  "document_type": "technical_book|textbook|reference|other"
}}
"""
        
        try:
            response = self.llm(prompt)
            # Try to parse JSON from response
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            return json.loads(response)
        except Exception:
            return {
                "title": "Unknown",
                "author": "Unknown",
                "num_chapters": 0,
                "chapter_titles": [],
                "document_type": "unknown",
            }
    
    def _summarize_chapters(self, text: str, structure: dict) -> dict[str, str]:
        """Generate per-chapter summaries (800-1200 tokens each)."""
        chapters = {}
        titles = structure.get("chapter_titles", [])
        
        # Split text into chunks (approximate chapters)
        # In production, you'd use the ToC to find exact boundaries
        chunk_size = len(text) // max(len(titles), 1)
        
        for i, title in enumerate(titles):
            start = i * chunk_size
            end = min((i + 1) * chunk_size, len(text))
            chunk = text[start:end]
            
            if len(chunk) < 100:
                continue
            
            prompt = f"""\
Summarize this chapter for a practitioner who needs to APPLY this knowledge.

CHAPTER: {title}
CONTENT: {chunk[:8000]}

Write a dense, actionable summary (800-1200 tokens):
- Lead with core mental models and principles
- Include specific techniques, algorithms, or patterns
- Use practitioner voice: "Use X when Y", not "The book explains X"
- Preserve exact terminology and framework names
- Include code examples if present (as markdown)
- End with "When to use:" and "Anti-patterns:" sections
"""
            
            try:
                summary = self.llm(prompt)
                slug = f"ch{i+1:02d}-{title.lower().replace(' ', '-')[:40]}"
                chapters[slug] = summary
            except Exception as e:
                logger.warning(f"Failed to summarize chapter {title}: {e}")
        
        return chapters
    
    def _extract_glossary(self, text: str, structure: dict) -> str:
        """Extract key terms with chapter references."""
        sample = text[:10000]
        
        prompt = f"""\
Extract all key technical terms from this document.

DOCUMENT SAMPLE:
{sample}

Create a glossary with:
- Term name
- Definition (1-2 sentences)
- Chapter reference

Format: **Term**: Definition. (Ch. N)

Include at least 30 terms. Focus on terms that a practitioner needs to know.
"""
        
        try:
            return self.llm(prompt)
        except Exception:
            return ""
    
    def _extract_patterns(self, text: str, structure: dict) -> str:
        """Extract techniques, algorithms, and design patterns."""
        sample = text[:10000]
        
        prompt = f"""\
Extract all techniques, algorithms, and design patterns from this document.

DOCUMENT SAMPLE:
{sample}

For each pattern, provide:
- **Name**: What it's called
- **When to use**: Applicability conditions
- **How it works**: 2-3 sentence description
- **Anti-patterns**: When NOT to use it

Format as a structured list. Include at least 15 patterns.
"""
        
        try:
            return self.llm(prompt)
        except Exception:
            return ""
    
    def _extract_cheatsheet(self, text: str, structure: dict) -> str:
        """Extract decision tables and quick-reference rules."""
        sample = text[:8000]
        
        prompt = f"""\
Create a quick-reference cheatsheet from this document.

DOCUMENT SAMPLE:
{sample}

Include:
1. Decision tables (if X, then do Y)
2. Quick-reference rules
3. Common gotchas and how to avoid them
4. Command/function reference (if applicable)

Format as markdown tables and bullet points. Keep it scannable.
"""
        
        try:
            return self.llm(prompt)
        except Exception:
            return ""
    
    def _generate_skill_md(
        self,
        structure: dict,
        chapters: dict,
        glossary: str,
        patterns: str,
        cheatsheet: str,
    ) -> str:
        """Generate the master SKILL.md with core mental models."""
        chapter_list = "\n".join(
            f"- {name}: {content[:200]}..."
            for name, content in list(chapters.items())[:10]
        )
        
        prompt = f"""\
Create the master SKILL.md for this document.

TITLE: {structure.get('title', 'Unknown')}
AUTHOR: {structure.get('author', 'Unknown')}
CHAPTERS: {list(chapters.keys())}

CORE MENTAL MODELS (extract from the document's key ideas):
[Generate 3-5 core mental models that capture the document's essence]

CHAPTER INDEX:
{chapter_list}

Write a SKILL.md that:
1. Opens with 3-5 core mental models (the most important ideas)
2. Includes a chapter index with one-line descriptions
3. Provides a topic-to-chapter mapping for on-demand loading
4. Lists the top 10 patterns/techniques
5. Includes a "When to reference this skill" section

Format: Markdown with clear section headers.
Keep under 4000 tokens. Front-load the most important content.
"""
        
        try:
            return self.llm(prompt)
        except Exception:
            return f"# {structure.get('title', 'Skill')}\n\n{chapter_list}"


# ═══════════════════════════════════════════════════════════
# INTEGRATED SELF-IMPROVEMENT PIPELINE
# ═══════════════════════════════════════════════════════════

class DragonSelfImprovement:
    """
    The complete self-improvement pipeline combining:
    1. Book-to-skill extraction (compile-time knowledge)
    2. SkillOpt training (feedback-driven optimization)
    3. Context management (progressive disclosure)
    4. Memory persistence (git-backed, survives compaction)
    
    This is what makes the system SMARTER over time.
    """
    
    def __init__(
        self,
        llm: Callable,
        workdir: str = ".",
        skill_dir: str = "~/.dragon/skills",
    ):
        self.llm = llm
        self.workdir = workdir
        self.skill_dir = Path(skill_dir).expanduser()
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        
        self.extractor = BookToSkillExtractor(llm)
        self.optimizer = SkillOptimizer(optimizer_model=llm, task_executor=None)
    
    def learn_from_document(
        self,
        document_path: str,
        skill_name: str = None,
        is_technical: bool = True,
    ) -> ExtractedSkill:
        """Phase 1: Extract structured knowledge from a document."""
        skill = self.extractor.extract(document_path, skill_name, is_technical)
        skill.save(str(self.skill_dir))
        return skill
    
    def improve_skill(
        self,
        skill_name: str,
        train_tasks: list[dict],
        val_tasks: list[dict],
        task_executor: Callable,
        config: SkillOptConfig = None,
    ) -> OptimizerState:
        """Phase 2: Train a skill using execution feedback."""
        # Load current best skill
        skill_path = self.skill_dir / skill_name / "SKILL.md"
        initial_skill = ""
        if skill_path.exists():
            initial_skill = skill_path.read_text()
        else:
            initial_skill = f"# {skill_name}\n\n[No skill content yet]"
        
        # Configure optimizer with task executor
        self.optimizer.executor = task_executor
        
        # Run SkillOpt training
        cfg = config if config is not None else SkillOptConfig()
        state = self.optimizer.train(
            skill_name=skill_name,
            initial_skill=initial_skill,
            train_tasks=train_tasks,
            val_tasks=val_tasks,
            config=cfg,
        )
        
        # Save best skill
        if state.best_skill:
            skill_path.write_text(state.best_skill)
            
            # Save training history
            history_path = self.skill_dir / skill_name / "training_history.json"
            with open(history_path, "w") as f:
                json.dump({
                    "best_score": state.best_validation_score,
                    "accepted_edits": len(state.accepted_edits),
                    "rejected_edits": len(state.rejected_edits),
                    "versions": [asdict(v) for v in state.skill_history],
                }, f, indent=2, default=str)
        
        return state
    
    def get_skill_for_context(self, topic: str) -> Optional[str]:
        """
        Progressive disclosure: Load only the skill content needed for this topic.
        Implements the book-to-skill on-demand loading pattern.
        """
        # Scan available skills
        for skill_dir in self.skill_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            
            metadata_path = skill_dir / "metadata.json"
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text())
                if topic.lower() in metadata.get("name", "").lower():
                    # Load SKILL.md (always in context)
                    skill_md = (skill_dir / "SKILL.md").read_text()
                    
                    # Check if there's a specific chapter for this topic
                    chapters_dir = skill_dir / "chapters"
                    if chapters_dir.exists():
                        for chapter_file in chapters_dir.iterdir():
                            if topic.lower() in chapter_file.stem.lower():
                                chapter_content = chapter_file.read_text()
                                return f"{skill_md}\n\n## Relevant Chapter\n{chapter_content}"
                    
                    return skill_md
        
        return None
