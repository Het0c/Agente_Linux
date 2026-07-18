#!/bin/bash

CONFIG="config_agents.json"

LLAMA_PATH=$(jq -r '.paths.llama_cpp' "$CONFIG")

LOG_DIR=$(jq -r '.paths.logs_dir' "$CONFIG")
[ -z "$LOG_DIR" ] && LOG_DIR="$HOME/llm_logs"
mkdir -p "$LOG_DIR"

echo "========================================"
echo "   AGENT SYSTEM - DEBUG MODE"
echo "========================================"

start_agent () {
    NAME=$1

    MODEL=$(jq -r ".${NAME}.model" "$CONFIG")
    PORT=$(jq -r ".${NAME}.port" "$CONFIG")
    CTX=$(jq -r ".${NAME}.ctx_size" "$CONFIG")
    THREADS=$(jq -r ".${NAME}.threads" "$CONFIG")
    GPU_LAYERS=$(jq -r ".${NAME}.n_gpu_layers" "$CONFIG")
    BATCH=$(jq -r ".${NAME}.batch_size" "$CONFIG")
    BACKEND=$(jq -r ".${NAME}.backend" "$CONFIG")

    LOG_FILE="$LOG_DIR/${NAME}.log"

    echo "[+] Starting $NAME"
    echo "    model : $MODEL"
    echo "    port  : $PORT"
    echo "    log   : $LOG_FILE"

    if [ "$BACKEND" = "cuda" ]; then
        CUDA_VISIBLE_DEVICES=0 \
        $LLAMA_PATH/build/bin/llama-server \
            -m "$MODEL" \
            --host 127.0.0.1 \
            --port "$PORT" \
            --ctx-size "$CTX" \
            --threads "$THREADS" \
            --n-gpu-layers "$GPU_LAYERS" \
            --batch-size "$BATCH" \
            --mlock \
            > "$LOG_FILE" 2>&1 &
    else
        $LLAMA_PATH/build/bin/llama-server \
            -m "$MODEL" \
            --host 127.0.0.1 \
            --port "$PORT" \
            --ctx-size "$CTX" \
            --threads "$THREADS" \
            --batch-size "$BATCH" \
            --n-gpu-layers "$GPU_LAYERS" \
            > "$LOG_FILE" 2>&1 &
    fi

    PID=$!
    echo "    pid   : $PID"

    # guardar PID
    echo $PID > "$LOG_DIR/${NAME}.pid"
}

# ===============================
# START AGENTS
# ===============================
start_agent "logic_agent"
start_agent "coder_agent"

echo "========================================"
echo "LOGIC  : http://localhost:8080"
echo "CODER  : http://localhost:8081"
echo "LOGDIR : $LOG_DIR"
echo "========================================"

# ===============================
# DEBUG STREAM (EN VIVO)
# ===============================
echo ""
echo ">>> LIVE DEBUG STREAM (Ctrl+C to exit)"
echo ""

konsole -e bash -c "tail -f '$LOG_DIR'/coder_agent.log; exec bash"
tail -f "$LOG_DIR"/logic_agent.log
