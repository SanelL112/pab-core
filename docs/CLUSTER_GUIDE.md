# Cluster Architecture & Operations Manual (LFM2.5 & DeepSeek Stack)

> **Primary Decision**: The **Surface Pro** (`sanel-lathiya`) is the **Main Orchestrator**, and its high-speed internal **SSD** (`/home/sanel-lathiya/models/`) is the **Primary Model Storage**.

---

## 1. Upgraded Model Stack Matrix

| Tier | Role | Old Model | Upgraded Model | Target Device | Why This Upgrade? |
|---|---|---|---|---|---|
| **Classifier** | 24/7 Intent & Safety Routing | `qwen2:0.5b` | **`LFM2.5-1.2B-Instruct`**<br>`(hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF)` | **Orange Pi 5** (`:8080` / `:11434`) | 200+ tok/s on ARM CPU, flawless JSON/schema compliance, no hallucinated classifications. |
| **Local Small** | Fast Local Fallback | `qwen2.5:3b` | **`LFM2.5-2.6B-Instruct (Q4_K_M)`**<br>`(hf.co/LiquidAI/LFM2.5-2.6B-Instruct-GGUF)` | **Orange Pi 5** (`:11434`) | Native 128K context, agentic structured tool calling in under 1.7 GB RAM. |
| **Local Large (RPC)** | Primary Heavy Inference & Reasoning | `qwen2.5-7b-instruct` | **`LFM2.5-8B-A1B`** or<br>**`DeepSeek-R1-Distill-Qwen-8B`** | **Surface Pro SSD** (`/home/sanel-lathiya/models/`) | MoE activates only 1B params per token (low heat/power), or DeepSeek for chain-of-thought reasoning. |
| **Fallback Ollama** | Emergency Host Fallback | `Qwen2-0.5B-GGUF` | **`LFM2.5-1.2B-Instruct`** | **Dell Laptop** (`127.0.0.1:11434`) | ~800 MB RAM footprint, robust instruction following. |

---

## 2. Hardware Topology & Network Map

```
               ┌────────────────────────────────────────────────────────┐
               │              Surface Pro (sanel-lathiya)               │
               │                  PRIMARY ORCHESTRATOR                  │
               │                                                        │
               │  • IP: 10.0.0.47 (WiFi) / 10.42.0.1 (USB Host)         │
               │  • Model: DeepSeek-R1-Distill-Qwen-8B / LFM2.5-8B-A1B  │
               │  • Storage: Fast NVMe SSD (/home/sanel-lathiya/models) │
               │  • Service: llama-server :8080 (OpenAI API)            │
               │  • Web UI: Cluster Manager :3000                       │
               └──────────────┬──────────────────────────┬──────────────┘
                              │                          │
           Offload RPC Layers │                          │ Offload RPC Layers
             (Gigabit/WiFi)   │                          │ (USB3 RNDIS / Ethernet)
                              ▼                          ▼
        ┌───────────────────────────┐      ┌───────────────────────────┐
        │  Dell Laptop (sanel, x86) │      │ Orange Pi 5 (RK3588, ARM) │
        │        RPC WORKER         │      │    RPC WORKER & EDGE      │
        │                           │      │                           │
        │ • IP: 10.10.10.1 / .0.61  │      │ • IP: 10.42.0.139 / .10.2 │
        │ • ggml-rpc-server :50052  │      │ • ggml-rpc-server :50052  │
        │ • Runs Bot 24/7           │      │ • Classifier Server :8080 │
        │ • Emergency LFM2.5-1.2B   │      │ • LFM2.5-1.2B & 2.6B      │
        │ • 773GB HDD Model Archive │      │ • Fast Edge Ollama :11434 │
        └───────────────────────────┘      └───────────────────────────┘
```

---

## 3. How to Pull / Download the New Models

### On Orange Pi 5 (Classifier & Fast Small Fallback):
```bash
ssh root@10.10.10.2
ollama pull hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF
ollama pull hf.co/LiquidAI/LFM2.5-2.6B-Instruct-GGUF
```

### On Dell Laptop (Emergency Host Fallback):
```bash
ollama pull hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF
```

### On Surface Pro (Primary Orchestrator 8B Models):
Download the GGUF directly to `/home/sanel-lathiya/models/`:
```bash
cd /home/sanel-lathiya/models/

# Option 1: DeepSeek R1 Distill Qwen 8B
wget -c "https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-8B-GGUF/resolve/main/DeepSeek-R1-Distill-Qwen-8B-Q4_K_M.gguf"

# Option 2: LFM2.5 8B MoE (A1B)
wget -c "https://huggingface.co/LiquidAI/LFM2.5-8B-A1B-GGUF/resolve/main/LFM2.5-8B-A1B-Q4_K_M.gguf"
```

---

## 4. Inference Routing Lifecycle

When a query arrives in Telegram, `personal-assistant-bot/llm_router.py` dispatches it through:

```
  1. Intent & Schema Classification (<0.2s)
     └── Evaluated by Pi Classifier (LFM2.5-1.2B-Instruct @ 10.10.10.2:8080)
         │
         ▼
  2. Surface Pro RPC Cluster (Primary 8B Inference)
     └── Hits http://10.0.0.47:8080/v1/chat/completions (DeepSeek-R1-8B or LFM2.5-8B)
     └── Surface offloads tensor layers across Surface CPU + Dell + Orange Pi
         │
         ├─► [SUCCESS]: Returns answer to user
         │
         ▼ [If Surface Pro is sleeping/offline]
  3. Orange Pi 5 Ollama (Fallback Tier 1)
     └── Calls http://10.10.10.2:11434 (LFM2.5-2.6B-Instruct)
         │
         ├─► [SUCCESS]: Returns answer to user
         │
         ▼ [If Orange Pi fails]
  4. Dell Local Ollama (Fallback Tier 2)
     └── Calls http://127.0.0.1:11434 (LFM2.5-1.2B-Instruct)
         │
         ├─► [SUCCESS]: Returns answer to user
         │
         ▼ [If all local options exhausted AND query is non-sensitive]
  5. Cloud OpenRouter (Fallback Tier 3)
     └── 1st choice: nvidia/nemotron-3-ultra-550b-a55b:free
     └── 2nd choice: nvidia/nemotron-3-nano-30b-a3b:free
     └── 3rd choice: tencent/hy3:free
```

---

## 5. Operations & Quick Commands

```bash
# 1. Check Surface Orchestrator & Loaded Model
curl -s http://10.0.0.47:8080/health
curl -s http://10.0.0.47:8080/v1/models | python3 -m json.tool

# 2. Run Test Prompt on Surface
curl -s http://10.0.0.47:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Explain RPC in 1 sentence."}],"max_tokens":32}'

# 3. Check Orange Pi Ollama Models
curl -s http://10.10.10.2:11434/api/tags
```
