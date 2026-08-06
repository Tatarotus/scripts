#!/usr/bin/env bash

set -euo pipefail


########################################
# Configuração
########################################

CACHE_DIR="$HOME/.cache/legendar"
SESSION_FILE="$CACHE_DIR/session"
TIMER_FILE="$CACHE_DIR/timer.pid"

mkdir -p "$CACHE_DIR"


########################################
# Cores
########################################

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
NC="\033[0m"


########################################
# Logs
########################################

info() {
    echo -e "${BLUE}==>${NC} $1"
}

ok() {
    echo -e "${GREEN}✔${NC} $1"
}

warn() {
    echo -e "${YELLOW}!${NC} $1"
}

error() {
    echo -e "${RED}✘${NC} $1"
    exit 1
}


########################################
# Verificações
########################################

check_dependencies() {

    command -v colab >/dev/null \
        || error "Colab CLI não encontrado"

    command -v ffmpeg >/dev/null \
        || error "ffmpeg não encontrado"

}


########################################
# Sessão
########################################

get_session() {

    if [ -f "$SESSION_FILE" ]; then

        SESSION=$(cat "$SESSION_FILE")

        if colab sessions | grep -q "$SESSION"; then
            echo "$SESSION"
            return
        fi

    fi


    echo ""

}


save_session() {

    echo "$1" > "$SESSION_FILE"

}


clear_session() {

    rm -f "$SESSION_FILE"

}


########################################
# Timer de desligamento
########################################

cancel_timer() {

    if [ -f "$TIMER_FILE" ]; then

        PID=$(cat "$TIMER_FILE")

        kill "$PID" 2>/dev/null || true

        rm -f "$TIMER_FILE"

    fi

}


start_timer() {

    cancel_timer


    (

        sleep 600


        if [ -f "$SESSION_FILE" ]; then

            SESSION=$(cat "$SESSION_FILE")

            echo
            info "10 minutos sem uso. Encerrando $SESSION..."

            colab stop -s "$SESSION" || true

            clear_session

        fi


    ) >/dev/null 2>&1 &


    echo $! > "$TIMER_FILE"

}

########################################
# Criar ou reutilizar Colab T4
########################################

ensure_session() {

    SESSION=$(get_session)


    if [ -n "$SESSION" ]; then

        ok "Usando sessão existente: $SESSION"

        return

    fi


    info "Criando nova sessão Colab T4..."


    OUTPUT=$(colab new --gpu T4 2>&1 || true)


    SESSION=$(echo "$OUTPUT" \
        | grep "Creating session" \
        | sed -E "s/.*'([^']+)'.*/\1/")


    if [ -z "$SESSION" ]; then

        error "Não foi possível criar sessão Colab"

    fi


    save_session "$SESSION"


    ok "Sessão criada: $SESSION"

}



########################################
# Verificar pacote remoto
########################################

remote_has_package() {

    PACKAGE="$1"


    cat > /tmp/check_package.py <<EOF
import importlib.util

print(
    importlib.util.find_spec("$PACKAGE")
    is not None
)
EOF


    RESULT=$(colab exec \
        -s "$SESSION" \
        -f /tmp/check_package.py \
        --timeout 60 2>/dev/null || true)


    echo "$RESULT" | grep -q "True"

}



########################################
# Instalar faster-whisper
########################################

ensure_whisper() {


    info "Verificando faster-whisper..."


    if remote_has_package "faster_whisper"; then

        ok "faster-whisper já instalado"

        return

    fi


    info "Instalando faster-whisper..."


    colab install \
        -s "$SESSION" \
        faster-whisper


    ok "faster-whisper instalado"

}

########################################
# Converter vídeo para Opus
########################################

prepare_audio() {

    INPUT="$1"
    NAME="${INPUT%.*}"

    AUDIO="${NAME}.opus"

    info "Convertendo áudio para Opus..."

    if [ -f "$AUDIO" ]; then

        info "Áudio já convertido, pulando..."

        return

    fi


    ffmpeg \
        -i "$INPUT" \
        -vn \
        -ac 1 \
        -ar 16000 \
        -c:a libopus \
        -b:a 32k \
        "$AUDIO" \
        -y


    ok "Criado: $AUDIO"

}



########################################
# Upload paralelo em chunks
########################################

upload_parallel() {

    local session="$1"
    local local_file="$2"
    local remote_path="$3"

    cat > /tmp/check_remote_file.py <<EOF
import os

print(os.path.exists("$remote_path"))
EOF

    local exists_result
    exists_result=$(colab exec -s "$session" -f /tmp/check_remote_file.py --timeout 60 2>/dev/null || true)
    rm -f /tmp/check_remote_file.py

    if echo "$exists_result" | grep -q "True"; then

        info "Arquivo remoto já existe no Colab, pulando..."

        return 0

    fi


    info "Enviando arquivo usando upload paralelo..."


    local tmp_dir
    tmp_dir=$(mktemp -d)

    trap 'rm -rf "$tmp_dir"' EXIT INT TERM


    split -b 10M "$local_file" "$tmp_dir/chunk_"


    local chunks=("$tmp_dir"/chunk_*)
    local num_chunks=${#chunks[@]}
    local max_jobs=10


    echo "Chunks:"
    echo "$num_chunks"
    echo "Threads:"
    echo "$max_jobs"


    local active_pids=()
    local pids=()
    declare -A pid_to_chunk

    for chunk in "${chunks[@]}"; do

        local chunk_name
        chunk_name=$(basename "$chunk")


        while true; do

            local still_running=()

            for p in "${active_pids[@]}"; do

                if kill -0 "$p" 2>/dev/null; then

                    still_running+=("$p")

                fi

            done

            active_pids=("${still_running[@]}")

            if [ ${#active_pids[@]} -lt $max_jobs ]; then

                break

            fi

            sleep 0.1

        done


        colab upload -s "$session" "$chunk" "/content/$chunk_name" >/dev/null 2>&1 &

        local pid=$!

        pids+=("$pid")

        active_pids+=("$pid")

        pid_to_chunk["$pid"]="$chunk_name"

    done


    for pid in "${pids[@]}"; do

        if ! wait "$pid"; then

            local failed_chunk="${pid_to_chunk[$pid]}"

            for p in "${pids[@]}"; do

                kill "$p" 2>/dev/null || true

            done

            rm -rf "$tmp_dir"

            error "Upload falhou no chunk: $failed_chunk"

        fi

    done


    rm -rf "$tmp_dir"

    trap - EXIT INT TERM


    info "Reconstruindo áudio no Colab..."


    cat > /tmp/reconstruct.py <<EOF
import glob
import os
import subprocess

chunks = sorted(glob.glob('/content/chunk_*'))
if not chunks:
    raise FileNotFoundError("Nenhum chunk encontrado no Colab")

output_path = """$remote_path"""

with open(output_path, 'wb') as outfile:
    subprocess.run(['cat'] + chunks, stdout=outfile, check=True)

for chunk in chunks:
    os.remove(chunk)

print("reconstructed")
EOF


    local rec_result
    rec_result=$(colab exec -s "$session" -f /tmp/reconstruct.py --timeout 300 2>/dev/null || true)
    rm -f /tmp/reconstruct.py


    if ! echo "$rec_result" | grep -q "reconstructed"; then

        error "Falha ao reconstruir áudio no Colab"

    fi

}



########################################
# Criar script remoto Whisper
########################################

create_remote_script() {


REMOTE_SCRIPT="transcribe_remote.py"


cat > "$REMOTE_SCRIPT" <<EOF
from faster_whisper import WhisperModel
import os


audio="/content/$(basename "$AUDIO")"

output="/content/$(basename "$SRT")"



if not os.path.exists(audio):
    raise FileNotFoundError(audio)



print("Carregando Whisper...")


model = WhisperModel(
    "large-v3",
    device="cuda",
    compute_type="float16"
)



print("Transcrevendo...")


segments, info = model.transcribe(
    audio,
    language="pt",
    beam_size=5,
    vad_filter=True
)



def tempo(segundos):

    h=int(segundos//3600)

    m=int((segundos%3600)//60)

    s=int(segundos%60)

    ms=int((segundos%1)*1000)


    return f"{h:02}:{m:02}:{s:02},{ms:03}"



with open(output,"w",encoding="utf-8") as f:


    for i,seg in enumerate(segments,1):

        f.write(
            f"{i}\\n"
            f"{tempo(seg.start)} --> {tempo(seg.end)}\\n"
            f"{seg.text.strip()}\\n\\n"
        )


print("Criado:", output)

EOF


ok "Script remoto criado"

}

########################################
# Executar Whisper remoto
########################################

run_whisper() {


    info "Enviando script Whisper..."


    colab upload \
        -s "$SESSION" \
        "$REMOTE_SCRIPT" \
        "/content/$REMOTE_SCRIPT"



    info "Rodando Whisper na T4..."


    colab exec \
        -s "$SESSION" \
        -f "$REMOTE_SCRIPT" \
        --timeout 7200



    ok "Transcrição finalizada"

}



########################################
# Baixar legenda
########################################

download_srt() {


    info "Baixando legenda..."


    colab download \
        -s "$SESSION" \
        "/content/$(basename "$SRT")" \
        "$SRT"



    ok "Legenda salva: $SRT"

}



########################################
# Limpeza remota
########################################

remote_cleanup() {


    info "Limpando arquivos temporários..."


    cat > /tmp/cleanup.py <<EOF
import os

files = [
    "/content/$(basename "$AUDIO")",
    "/content/$(basename "$SRT")",
    "/content/$REMOTE_SCRIPT"
]


for f in files:

    if os.path.exists(f):
        os.remove(f)

print("clean")

EOF



    colab exec \
        -s "$SESSION" \
        -f /tmp/cleanup.py \
        --timeout 60 \
        >/dev/null 2>&1 || true


}



########################################
# Status
########################################

show_status() {


    echo


    if [ -f "$SESSION_FILE" ]; then

        SESSION=$(cat "$SESSION_FILE")


        echo "Sessão:"
        echo "$SESSION"


        echo

        colab status -s "$SESSION" || true


    else

        warn "Nenhuma sessão ativa"

    fi

}



########################################
# Parar sessão manualmente
########################################

stop_session() {


    if [ ! -f "$SESSION_FILE" ]; then

        warn "Nenhuma sessão salva"

        exit 0

    fi



    SESSION=$(cat "$SESSION_FILE")


    info "Encerrando $SESSION..."


    colab stop \
        -s "$SESSION" || true



    clear_session


    ok "Sessão encerrada"

}

########################################
# Ajuda
########################################

show_help() {

cat <<EOF

Uso:

  $0 arquivo.ts|arquivo.mp4|arquivo.mkv

Comandos:

  $0 status
      Mostra sessão atual

  $0 stop
      Encerra sessão Colab

Exemplo:

  $0 "Aula 07 - Analise.mp4"

EOF

}



########################################
# Main
########################################

main() {


    if [ $# -eq 0 ]; then

        show_help

        exit 1

    fi



    COMMAND="$1"



    case "$COMMAND" in


        status)

            show_status

            exit 0

        ;;



        stop)

            stop_session

            exit 0

        ;;



        -h|--help|help)

            show_help

            exit 0

        ;;


    esac



    INPUT="$1"



    if [ ! -f "$INPUT" ]; then

        error "Arquivo não encontrado: $INPUT"

    fi



    NAME="${INPUT%.*}"

    AUDIO="${NAME}.opus"

    SRT="${NAME}.srt"



    check_dependencies



    cancel_timer



    ensure_session



    ensure_whisper



    prepare_audio "$INPUT"



    upload_parallel "$SESSION" "$AUDIO" "/content/$(basename "$AUDIO")"



    create_remote_script



    run_whisper



    download_srt



    remote_cleanup



    start_timer



    echo

    echo "======================================"

    ok "Concluído!"

    echo

    echo "Legenda:"
    echo "$SRT"

    echo

    echo "Sessão ficará ativa por 10 minutos."

    echo "Use:"

    echo "  $0 stop"

    echo

    echo "======================================"


}



main "$@"
