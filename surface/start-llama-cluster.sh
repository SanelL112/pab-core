#!/bin/bash
MODEL_PATH="${MODEL_PATH:-/home/sanel-lathiya/models/LFM2.5-2.6B-Q8_0.gguf}"
CONTEXT_SIZE="${CONTEXT_SIZE:-4096}"

RPC_DELL=""
if ping -c 1 -W 1 10.10.10.1 &> /dev/null; then
    RPC_DELL="10.10.10.1:50052"
elif ping -c 1 -W 1 10.0.0.61 &> /dev/null; then
    RPC_DELL="10.0.0.61:50052"
fi

RPC_PI=""
if ping -c 1 -W 1 10.42.0.139 &> /dev/null; then
    RPC_PI="10.42.0.139:50052"
fi

RPC_ARGS=""
if [ -n "$RPC_DELL" ] && [ -n "$RPC_PI" ]; then
    RPC_ARGS="--rpc $RPC_DELL,$RPC_PI"
elif [ -n "$RPC_DELL" ]; then
    RPC_ARGS="--rpc $RPC_DELL"
elif [ -n "$RPC_PI" ]; then
    RPC_ARGS="--rpc $RPC_PI"
fi

echo "Initiating llama.cpp across full RPC Cluster to maximize compute offloading!"
exec /home/sanel-lathiya/llama.cpp/build/bin/llama-server -m "$MODEL_PATH" $RPC_ARGS -c "$CONTEXT_SIZE" --host 0.0.0.0 --port 8080
