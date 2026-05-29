# 🐉 Dragon Hive Mind v1.0

Multi-agent compute mesh orchestrator for E5 Enclave.

## Architecture

```
Hermie (Visionary) → Dragon Orchestrator → NATS JetStream → Agent Workers
                                           → Ollama (MC RTX 3070)
                                           → Ollama (WSL2)
                                           → HF Spaces (free GPU)
```

## Quick Start

```bash
pip install asyncio nats-py httpx
python dragon/dragon_hive_mind.py
```

## Compute Tiers

- `LOCAL_GPU` — MC RTX 3070 (llama3.1:8b, qwen2.5:7b, mistral:7b)
- `LOCAL_CPU` — WSL2 CPU inference
- `HF_SPACE` — HuggingFace Spaces free Ollama backends
- `CLOUDFLARE` — CF Workers edge compute
- `CLOUD_FREE` — Oracle Cloud ARM VM, Kaggle, Colab
- `ORPHAN_FTP` — Hunted orphan compute (experimental)

Built per Chairman directive: "Think outside the box. Push past HuggingFace, push past Amazon. You can push through anything."
