#!/bin/bash
MODEL_PATH="${MODEL_PATH:-/home/sanel-lathiya/models/LFM2.5-2.6B-Q8_0.gguf}"
# 64K context: LFM2.5-2.6B supports up to 128K, but KV cache is distributed
# across the RPC backends (Surface+Pi+Dell) and the Dell node is RAM-tight, so
# 64K + flash-attn + q8_0 KV quant is the measured safe ceiling. Override via env.
CONTEXT_SIZE="${CONTEXT_SIZE:-65536}"
THREADS="${THREADS:-6}"
TENSOR_SPLIT="${TENSOR_SPLIT:-4,6}"

RPC_DELL=""
if timeout 1 bash -c '</dev/tcp/10.10.10.1/50052' 2>/dev/null; then
    RPC_DELL="10.10.10.1:50052"
elif timeout 1 bash -c '</dev/tcp/10.0.0.61/50052' 2>/dev/null; then
    RPC_DELL="10.0.0.61:50052"
fi

RPC_PI=""
if timeout 1 bash -c '</dev/tcp/10.42.0.139/50052' 2>/dev/null; then
    RPC_PI="10.42.0.139:50052"
elif timeout 1 bash -c '</dev/tcp/10.10.10.2/50052' 2>/dev/null; then
    RPC_PI="10.10.10.2:50052"
fi

RPC_ARGS=""
if [ -n "$RPC_DELL" ] && [ -n "$RPC_PI" ]; then
    RPC_ARGS="--rpc $RPC_PI,$RPC_DELL"
elif [ -n "$RPC_PI" ]; then
    RPC_ARGS="--rpc $RPC_PI"
elif [ -n "$RPC_DELL" ]; then
    RPC_ARGS="--rpc $RPC_DELL"
fi

SPLIT_ARGS=""
if [ -n "$TENSOR_SPLIT" ] && [ -n "$RPC_DELL" ] && [ -n "$RPC_PI" ]; then
    SPLIT_ARGS="--tensor-split $TENSOR_SPLIT"
fi

echo "Initiating llama.cpp cluster on Surface orchestrator!"
echo "Model: $MODEL_PATH"
echo "RPC Workers: $RPC_ARGS"
[ -n "$SPLIT_ARGS" ] && echo "Tensor Split: $SPLIT_ARGS"

exec /home/sanel-lathiya/llama.cpp/build/bin/llama-server \
    -m "$MODEL_PATH" \
    -c "$CONTEXT_SIZE" \
    -t "$THREADS" \
    $RPC_ARGS \
    $SPLIT_ARGS \
    --host 0.0.0.0 \
    --port 8080 \
    --flash-attn on \
    --cache-type-k q8_0 \
    --cache-type-v q8_0
