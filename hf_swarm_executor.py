"""
HF Spaces Swarm Executor v1.1 — Production
===========================================
Auto-discovers live Ollama spaces via HF API, then fans out
inference tasks across all of them in parallel.

Verified working spaces (2026-05-28):
- mano-wii/ollama (qwen2.5:0.5b) — 1.8s latency
- Deepak7376/ollama-server (deepseek-r1:1.5b) — 10.8s
- zurikoff/ollama-server (qwen2.5-coder:0.5b) — 0.9s
- Kazilsky/Ollama (hermes3:latest) — 4.9s
- yashdubey/lexora-ollama (qwen2.5:1.5b) — 1.7s
- SumanEnv26/OllamaSu (qwen2.5:0.5b) — 1.6s
- Ayanpro/my-ollama-test (deepseek-coder:1.3b) — 6.2s
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("hf_swarm")

# ═══════════════════════════════════════════════════════════
# KNOWN WORKING OLLAMA SPACES (verified 2026-05-28)
# ═══════════════════════════════════════════════════════════

VERIFIED_OLLAMA_SPACES = [
    "mano-wii/ollama",
    "Deepak7376/ollama-server",
    "zurikoff/ollama-server",
    "Kazilsky/Ollama",
    "yashdubey/lexora-ollama",
    "SumanEnv26/OllamaSu",
    "Ayanpro/my-ollama-test",
]

# Candidate sleeping spaces (need warmup via HTTP hit first)
CANDIDATE_SPACES = [
    "Gershonbest/ollama-inference",
    "Mmfallah/ollama-server",
    "BasToTheMax/ollama",
    "Echo-AI-official/ollama-gemma3",
    "terrencemiao/Ollama",
    "SonLe/ollama-tinkering",
    "abhinand/ollama-server",
    "moamen270/Ollama",
    "gingdev/ollama-server",
    "Hanchin/ollama",
    "faircompute/ollama",
    "imrohankataria/ollama-server",
    "Yaya86/ollama-server",
    "mahiatlinux/ollama-server-backend",
    "brogalan/ollama",
    "robinroy03/ollama-server-backend",
]


# ═══════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════

@dataclass
class SwarmTask:
    id: str
    prompt: str
    model: str = ""
    assigned_space: str = ""
    result: str = ""
    error: str = ""
    latency_ms: float = 0.0
    attempts: int = 0
    done: bool = False


@dataclass 
class SpaceEndpoint:
    space_id: str
    url: str
    models: list[str] = field(default_factory=list)
    online: bool = False
    latency_ms: float = 0.0
    total_requests: int = 0
    ok_requests: int = 0
    fail_requests: int = 0

    @staticmethod
    def from_id(space_id: str) -> "SpaceEndpoint":
        user, name = space_id.split("/")
        slug = f"{user}-{name}".lower().replace("_", "-")
        return SpaceEndpoint(space_id=space_id, url=f"https://{slug}.hf.space")


# ═══════════════════════════════════════════════════════════
# DISCOVERY ENGINE
# ═══════════════════════════════════════════════════════════

async def discover_spaces(
    verified: list[str] = None,
    candidates: list[str] = None,
    probe_timeout: float = 12.0,
    max_concurrent: int = 20,
) -> list[SpaceEndpoint]:
    """
    Full discovery pipeline:
    1. Probe all verified spaces (fast path)
    2. Probe candidates in parallel (warmup sleeping spaces)
    3. Return all that responded with Ollama API
    """
    if verified is None:
        verified = list(VERIFIED_OLLAMA_SPACES)
    if candidates is None:
        candidates = list(CANDIDATE_SPACES)
    
    async def _probe_one(space_id: str, timeout: float) -> SpaceEndpoint:
        ep = SpaceEndpoint.from_id(space_id)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{ep.url}/api/tags")
                if r.status_code == 200:
                    data = r.json()
                    ep.models = [m.get("name", "") for m in data.get("models", [])]
                    ep.online = len(ep.models) > 0
                    ep.latency_ms = float(r.headers.get("x-runtime", 0)) * 1000
                    if ep.latency_ms == 0:
                        ep.latency_ms = 1000  # Default if header not present
        except:
            pass
        return ep
    
    sem = asyncio.Semaphore(max_concurrent)
    async def _p(sid, t):
        async with sem:
            return await _probe_one(sid, t)
    
    # Probe verified first (these should be alive)
    logger.info(f"Probing {len(verified)} verified + {len(candidates)} candidate spaces...")
    all_ids = verified + candidates
    results = await asyncio.gather(*[_p(sid, probe_timeout) for sid in all_ids])
    
    alive = [ep for ep in results if ep.online]
    logger.info(f"Discovered {len(alive)}/{len(all_ids)} alive Ollama spaces")
    for ep in alive:
        models_str = ", ".join(ep.models[:2])
        logger.info(f"  [OK] {ep.space_id:45s} [{models_str}]")
    
    return alive


# ═══════════════════════════════════════════════════════════
# SWARM EXECUTOR
# ═══════════════════════════════════════════════════════════

class HFSwarmExecutor:
    """Production HF Spaces swarm executor with auto-discovery and load balancing."""
    
    def __init__(self, inference_timeout: float = 60.0, probe_timeout: float = 12.0):
        self.inference_timeout = inference_timeout
        self.probe_timeout = probe_timeout
        self.spaces: list[SpaceEndpoint] = []
        self._rr = 0
    
    async def discover(self) -> list[SpaceEndpoint]:
        """Discover alive spaces. Run this before swarm()."""
        self.spaces = await discover_spaces(probe_timeout=self.probe_timeout)
        return self.spaces
    
    def _pick_space(self) -> Optional[SpaceEndpoint]:
        """Round-robin space selection."""
        alive = [s for s in self.spaces if s.online]
        if not alive:
            return None
        space = alive[self._rr % len(alive)]
        self._rr += 1
        return space
    
    def _pick_model(self, space: SpaceEndpoint, preferred: str = "") -> str:
        """Pick best model from a space."""
        if not space.models:
            return ""
        if preferred and preferred in space.models:
            return preferred
        # Prefer smaller models for speed
        small = [m for m in space.models if any(x in m for x in ["0.5b", "1.5b", "1b"])]
        return small[0] if small else space.models[0]
    
    async def execute_one(self, task: SwarmTask, retries: int = 4) -> SwarmTask:
        """Execute a single task with automatic failover across spaces."""
        t0 = time.monotonic()
        
        for attempt in range(retries):
            space = self._pick_space()
            if not space:
                task.error = "No alive spaces"
                task.done = True
                return task
            
            task.attempts += 1
            task.assigned_space = space.space_id
            model = self._pick_model(space, task.model)
            
            try:
                async with httpx.AsyncClient(timeout=self.inference_timeout) as client:
                    resp = await client.post(
                        f"{space.url}/api/generate",
                        json={
                            "model": model,
                            "prompt": task.prompt,
                            "stream": False,
                            "options": {"num_predict": 512, "temperature": 0.5}
                        }
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        task.result = data.get("response", str(data))
                        task.error = ""
                        task.done = True
                        task.latency_ms = (time.monotonic() - t0) * 1000
                        space.ok_requests += 1
                        space.total_requests += 1
                        logger.debug(f"[OK] {task.id} -> {space.space_id} ({task.latency_ms:.0f}ms)")
                        return task
                    else:
                        space.fail_requests += 1
                        space.total_requests += 1
                        logger.debug(f"[WARN] {task.id} HTTP {resp.status_code} on {space.space_id}")
                        
            except httpx.TimeoutException:
                space.fail_requests += 1
                space.total_requests += 1
                logger.debug(f"[TIME] {task.id} timeout on {space.space_id}")
            except Exception as e:
                space.fail_requests += 1
                space.total_requests += 1
                logger.debug(f"[ERR] {task.id} on {space.space_id}: {str(e)[:60]}")
        
        task.error = f"All {retries} attempts failed"
        task.done = True
        task.latency_ms = (time.monotonic() - t0) * 1000
        return task
    
    async def swarm(self, tasks: list[SwarmTask], max_concurrent: int = 10) -> list[SwarmTask]:
        """Execute tasks in parallel across all alive spaces."""
        if not self.spaces:
            await self.discover()
        
        alive = [s for s in self.spaces if s.online]
        if not alive:
            for t in tasks:
                t.error = "No alive spaces"
                t.done = True
            return tasks
        
        logger.info(f"[SWARM] {len(tasks)} tasks -> {len(alive)} spaces (concurrency={max_concurrent})")
        
        sem = asyncio.Semaphore(max_concurrent)
        async def _run(t):
            async with sem:
                return await self.execute_one(t)
        
        t0 = time.monotonic()
        results = await asyncio.gather(*[_run(t) for t in tasks])
        elapsed = time.monotonic() - t0
        
        completed = sum(1 for t in results if t.result)
        failed = sum(1 for t in results if t.error)
        avg_lat = sum(t.latency_ms for t in results if t.result) / max(completed, 1)
        
        logger.info(f"[DONE] {completed}/{len(tasks)} done, {failed} failed | {elapsed:.1f}s total | {avg_lat:.0f}ms avg | {completed/max(elapsed, 0.01):.1f} t/s")
        
        return results
    
    def health_report(self) -> str:
        """Print health status of all known spaces."""
        lines = ["\n=== HF Spaces Health ==="]
        for s in self.spaces:
            rate = s.ok_requests / max(s.total_requests, 1) * 100
            models = ", ".join(s.models[:2]) or "?"
            status = "ONLINE" if s.online else "offline"
            lines.append(f"  {s.space_id:45s} {status:8s} {rate:.0f}% ok ({s.total_requests} req) [{models}]")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# HF API DISCOVERY — Find new spaces dynamically
# ══════════════════════════════════════════════════════════=

async def find_ollama_spaces_via_api(limit: int = 500) -> list[str]:
    """Query HF API for all Ollama-related spaces."""
    all_ids = []
    async with httpx.AsyncClient(timeout=30) as c:
        for offset in range(0, limit, 100):
            r = await c.get(
                "https://huggingface.co/api/spaces",
                params={"search": "ollama", "limit": 100, "offset": offset, "sort": "likes", "direction": -1}
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            for s in data:
                sid = s.get("id", "")
                if sid and sid not in all_ids:
                    all_ids.append(sid)
    return all_ids


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  HF SPACES SWARM EXECUTOR v1.1")
    print("  Free Parallel GPU Inference Mesh")
    print("=" * 60)
    
    executor = HFSwarmExecutor(inference_timeout=60.0)
    
    # Phase 1: Discovery
    print("\n[1/3] Discovering live HF Ollama spaces...")
    spaces = await executor.discover()
    
    if not spaces:
        print("\n  No spaces online. Possible causes:")
        print("  - HF Spaces sleeping (need warmup)")
        print("  - Network restrictions")
        print("  - API rate limiting")
        return
    
    print(f"\n  {len(spaces)} spaces ready for inference")
    
    # Phase 2: Single test
    print(f"\n[2/3] Single inference test...")
    test = SwarmTask(id="test1", prompt="Reply with exactly: swarm active")
    result = await executor.execute_one(test)
    if result.result:
        print(f"  [OK] {result.result.strip()[:80]}")
        print(f"       via {result.assigned_space} in {result.latency_ms:.0f}ms")
    else:
        print(f"  [--] {result.error}")
    
    # Phase 3: Swarm benchmark
    print(f"\n[3/3] Swarm benchmark (10 parallel tasks)...")
    bench_tasks = [
        SwarmTask(id=f"b{i}", prompt=f"What is {i}+{i}? Answer with just the number.")
        for i in range(1, 11)
    ]
    results = await executor.swarm(bench_tasks, max_concurrent=10)
    
    print("\n  Results:")
    for t in results:
        icon = "[OK]" if t.result else "[--]"
        text = (t.result or "")[:50].strip() if t.result else (t.error or "?")
        space = t.assigned_space or "?"
        print(f"    {icon} {t.id}: {text}  ({space}, {t.latency_ms:.0f}ms)")
    
    # Health
    print(executor.health_report())
    print(f"\nHF Swarm Executor ready.")

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    asyncio.run(main())
