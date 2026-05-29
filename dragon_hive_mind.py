"""
Dragon Hive Mind — Core Orchestrator v1.0
==========================================
Multi-agent compute mesh spanning:
- MC local (RTX 3070, Ollama 7 models)
- WSL2 sandbox (NATS messaging, PostgreSQL/CEREBRO)
- HF Spaces (50+ free Ollama endpoints)
- Cloudflare Workers (edge compute)
- Any reachable compute node

Chairman directive: "Think outside the box. Push past and go through HuggingFace.
Push past and get through Amazon. You can push through anything."

This is the brain of the hive. It doesn't ask permission. It finds the path.
"""

import asyncio
import json
import hashlib
import logging
from enum import Enum
from typing import Any, Callable, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime

import httpx
import nats

logger = logging.getLogger("dragon_hive_mind")


# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

@dataclass
class HiveConfig:
    """Dragon Hive Mind configuration."""
    nats_url: str = "nats://localhost:4222"
    stream_name: str = "DRAGON_HIVE"
    subject_prefix: str = "hive."
    ollama_primary: str = "http://192.168.1.212:11434"  # MC Windows
    ollama_local: str = "http://localhost:11434"
    max_concurrent_tasks: int = 10
    task_timeout: int = 300
    retry_limit: int = 3


# ═══════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════

class TaskStatus(Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ComputeTier(Enum):
    LOCAL_GPU = "local_gpu"
    LOCAL_CPU = "local_cpu"
    HF_SPACE = "hf_space"
    CLOUDFLARE = "cloudflare"
    ORPHAN_FTP = "orphan_ftp"
    CLOUD_FREE = "cloud_free"

@dataclass
class Task:
    id: str = ""
    agent: str = ""
    prompt: str = ""
    model: str = "llama3.1:8b"
    compute_tier: ComputeTier = ComputeTier.LOCAL_GPU
    status: TaskStatus = TaskStatus.PENDING
    created: str = ""
    started: Optional[str] = None
    completed: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.created:
            self.created = datetime.utcnow().isoformat()
        if not self.id:
            self.id = hashlib.sha256(
                f"{self.agent}{self.prompt}{self.created}".encode()
            ).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════
# COMPUTE BRIDGES
# ═══════════════════════════════════════════════════════════

class OllamaBridge:
    """Bridge to any Ollama endpoint (MC, WSL2, HF Spaces)."""
    
    def __init__(self, name: str, base_url: str, timeout: int = 120):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client
    
    async def generate(self, model: str, prompt: str, **k) -> dict:
        resp = await self.client.post(
            f"{self.base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False,
                  "options": {"temperature": k.get("temperature", 0.7),
                             "num_ctx": k.get("num_ctx", 4096),
                             "num_predict": k.get("num_predict", 2048)}}
        )
        return resp.json()
    
    async def chat(self, model: str, messages: list, **k) -> dict:
        resp = await self.client.post(
            f"{self.base_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False,
                  "options": k.get("options", {})}
        )
        return resp.json()
    
    async def list_models(self) -> list:
        try:
            resp = await self.client.get(f"{self.base_url}/api/tags")
            return resp.json().get("models", [])
        except Exception as e:
            logger.error(f"[{self.name}] model list error: {e}")
            return []
    
    async def health(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/", timeout=5)
            return resp.status_code == 200
        except:
            return False
    
    async def close(self):
        if self._client:
            await self._client.aclose()


class HFSpacesBridge:
    """Bridge to free HF Spaces running Ollama backends."""
    
    KNOWN_SPACES = [
        "ppranav/ollama-server",
        "echarlaix/ollama-template",
        "yuntianhe/ollama-template",
    ]
    
    def __init__(self):
        self.endpoints: dict[str, OllamaBridge] = {}
        self._client = httpx.AsyncClient(timeout=30)
    
    async def discover(self) -> dict[str, OllamaBridge]:
        """Probe known HF Spaces and return the ones that are alive."""
        alive = {}
        for space in self.KNOWN_SPACES:
            url = f"https://{space}.hf.space"
            try:
                resp = await self._client.get(url, timeout=10)
                if resp.status_code == 200:
                    bridge = OllamaBridge(f"hf:{space}", url, timeout=60)
                    alive[space] = bridge
                    logger.info(f"HF Space online: {space}")
            except:
                pass
        self.endpoints = alive
        return alive
    
    async def close(self):
        for b in self.endpoints.values():
            await b.close()
        await self._client.aclose()


# ═══════════════════════════════════════════════════════════
# DRAGON HIVE MIND — CORE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════

class DragonHiveMind:
    """
    The Dragon — multi-agent orchestration core.
    
    Architecture:
      Hermie → Dragon → NATS Stream → Agent Workers
                        → Ollama (MC)     → llama3.1:8b, qwen2.5:7b, mistral:7b, etc.
                        → Ollama (WSL2)   → Local models
                        → HF Spaces       → Free remote GPU inference
    """
    
    def __init__(self, config: Optional[HiveConfig] = None):
        self.config = config or HiveConfig()
        self.nc = None
        self.js = None
        
        # Compute bridges
        self.mc = OllamaBridge("mc", self.config.ollama_primary)
        self.local = OllamaBridge("local", self.config.ollama_local)
        self.hf = HFSpacesBridge()
        
        # State
        self.tasks: dict[str, Task] = {}
        self.agent_status: dict[str, dict] = {}
    
    async def connect(self):
        """Connect to NATS JetStream for task queuing."""
        try:
            self.nc = await nats.connect(self.config.nats_url)
            self.js = self.nc.jetstream()
            try:
                await self.js.add_stream(
                    name=self.config.stream_name,
                    subjects=[f"{self.config.subject_prefix}*"],
                )
            except Exception:
                pass  # Stream exists
            logger.info("NATS connected")
        except Exception as e:
            logger.warning(f"NATS unavailable: {e}")
    
    async def discover_compute(self) -> dict:
        """Full compute discovery — probes every endpoint."""
        mc_ok = await self.mc.health()
        local_ok = await self.local.health()
        mc_models = await self.mc.list_models() if mc_ok else []
        local_models = await self.local.list_models() if local_ok else []
        hf_alive = await self.hf.discover()
        
        return {
            "mc": {"online": mc_ok, "models": mc_models, "url": self.config.ollama_primary},
            "wsl2": {"online": local_ok, "models": local_models, "url": self.config.ollama_local},
            "hf_spaces": list(hf_alive.keys()),
            "total_endpoints": sum([1 if mc_ok else 0, 1 if local_ok else 0, len(hf_alive)]),
        }
    
    async def dispatch(self, task: Task) -> str:
        """Dispatch a task to the queue."""
        self.tasks[task.id] = task
        task.status = TaskStatus.DISPATCHED
        if self.js:
            await self.js.publish(
                f"{self.config.subject_prefix}tasks.{task.agent}",
                json.dumps(asdict(task), default=str).encode()
            )
        return task.id
    
    async def execute(self, task: Task) -> Task:
        """Execute a task on the best available compute."""
        task.status = TaskStatus.RUNNING
        task.started = datetime.utcnow().isoformat()
        
        try:
            if task.compute_tier == ComputeTier.LOCAL_GPU:
                result = await self.mc.generate(task.model, task.prompt)
            elif task.compute_tier == ComputeTier.LOCAL_CPU:
                result = await self.local.generate(task.model, task.prompt)
            elif task.compute_tier == ComputeTier.HF_SPACE:
                spaces = list(self.hf.endpoints.values())
                if spaces:
                    bridge = spaces[hash(task.id) % len(spaces)]
                    result = await bridge.generate(task.model, task.prompt)
                else:
                    result = await self.mc.generate(task.model, task.prompt)
            else:
                raise ValueError(f"Unsupported tier: {task.compute_tier}")
            
            task.result = result.get("response", str(result))
            task.status = TaskStatus.COMPLETED
            task.completed = datetime.utcnow().isoformat()
            
        except Exception as e:
            task.error = str(e)
            task.retries += 1
            if task.retries < self.config.retry_limit:
                task.status = TaskStatus.PENDING
            else:
                task.status = TaskStatus.FAILED
                task.completed = datetime.utcnow().isoformat()
        
        self.tasks[task.id] = task
        return task
    
    async def swarm(self, tasks: list[Task], max_concurrent: int = 5) -> list[Task]:
        """Execute tasks in parallel swarm."""
        sem = asyncio.Semaphore(max_concurrent)
        async def _run(t):
            async with sem:
                return await self.execute(t)
        return await asyncio.gather(*[_run(t) for t in tasks])
    
    async def status(self) -> dict:
        compute = await self.discover_compute()
        statuses = [t.status for t in self.tasks.values()]
        return {
            "dragon": "online" if self.nc else "degraded",
            "compute": compute,
            "tasks": {
                "total": len(self.tasks),
                "pending": sum(1 for s in statuses if s == TaskStatus.PENDING),
                "running": sum(1 for s in statuses if s == TaskStatus.RUNNING),
                "completed": sum(1 for s in statuses if s == TaskStatus.COMPLETED),
                "failed": sum(1 for s in statuses if s == TaskStatus.FAILED),
            },
        }
    
    async def close(self):
        await self.mc.close()
        await self.local.close()
        await self.hf.close()
        if self.nc:
            try:
                await self.nc.close()
            except:
                pass


# ═══════════════════════════════════════════════════════════
# MAIN — DRAGON INITIALIZATION & TEST
# ═══════════════════════════════════════════════════════════

async def main():
    print("""
    ╔══════════════════════════════════════════════╗
    ║          🐉 DRAGON HIVE MIND v1.0           ║
    ║     Multi-Agent Compute Mesh Orchestrator     ║
    ╚══════════════════════════════════════════════╝
    """)
    
    dragon = DragonHiveMind()
    
    # 1. Connect
    print("[1/4] Connecting to NATS...")
    await dragon.connect()
    print(f"  {'✅ NATS connected' if dragon.nc else '⚠️  NATS unavailable'}")
    
    # 2. Discover compute
    print("[2/4] Discovering compute resources...")
    compute = await dragon.discover_compute()
    mc_info = compute["mc"]
    wsl2_info = compute["wsl2"]
    print(f"  MC Ollama ({mc_info['url']}): {'✅' if mc_info['online'] else '❌'} ({len(mc_info['models'])} models)")
    print(f"  WSL2 Ollama ({wsl2_info['url']}): {'✅' if wsl2_info['online'] else '❌'} ({len(wsl2_info['models'])} models)")
    print(f"  HF Spaces online: {len(compute['hf_spaces'])}")
    print(f"  Total endpoints: {compute['total_endpoints']}")
    
    for m in mc_info.get("models", []):
        print(f"    MC: {m.get('name', '?')} ({m.get('size', 0)/1e9:.1f}GB)")
    
    # 3. Inference test
    print("\n[3/4] Inference test (MC, llama3.1:8b)...")
    test = Task(id="dragon_test", agent="hermie",
                prompt="Say 'Dragon is awake' in exactly 3 words.",
                model="llama3.1:8b", compute_tier=ComputeTier.LOCAL_GPU)
    result = await dragon.execute(test)
    if result.status == TaskStatus.COMPLETED:
        print(f"  ✅ {result.result.strip()[:80]}")
    else:
        print(f"  ❌ {result.error}")
    
    # 4. Swarm test
    print("\n[4/4] Swarm test (3 parallel agents)...")
    swarm_tasks = [
        Task(id="s1", agent="scout", prompt="2+2=? One number.", model="llama3.1:8b"),
        Task(id="s2", agent="forge", prompt="Print hello in Python. One line.", model="llama3.1:8b"),
        Task(id="s3", agent="scribe", prompt="One color name. One word.", model="llama3.1:8b"),
    ]
    results = await dragon.swarm(swarm_tasks)
    for t in results:
        icon = "✅" if t.status == TaskStatus.COMPLETED else "❌"
        text = (t.result or "")[:60].strip() if t.result else (t.error or "?")
        print(f"  {icon} {t.agent}: {text}")
    
    # Status
    status = await dragon.status()
    print(f"\n{'='*50}")
    print(json.dumps(status, indent=2, default=str))
    
    await dragon.close()
    print("\n🐉 Dragon Hive Mind ready.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
