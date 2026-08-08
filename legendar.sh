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

    if [ "${USE_VIDEO:-0}" -eq 0 ]; then
        command -v ffmpeg >/dev/null \
            || error "ffmpeg não encontrado"
    fi

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

    local input="$1"
    local name="${input%.*}"
    local audio="${name}.opus"

    info "Convertendo áudio para Opus..."

    if [ -f "$audio" ]; then

        info "Áudio já convertido, pulando..."

        return 0

    fi


    if ! ffmpeg \
        -i "$input" \
        -vn \
        -ac 1 \
        -ar 16000 \
        -c:a libopus \
        -b:a 32k \
        "$audio" \
        -y; then

        warn "Falha na conversão FFmpeg para: $input"

        return 1

    fi


    ok "Criado: $audio"

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

            warn "Upload falhou no chunk: $failed_chunk"

            return 1

        fi

    done


    rm -rf "$tmp_dir"

    trap - EXIT INT TERM


    info "Reconstruindo arquivo no Colab..."


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

        warn "Falha ao reconstruir arquivo no Colab"

        return 1

    fi

}



########################################
# Criar script remoto Whisper
########################################

create_remote_script() {

    local target_media="$1"
    local target_srt="$2"
    local target_txt="${3:-}"
    local gen_txt="${4:-0}"

    REMOTE_SCRIPT="transcribe_remote.py"


cat > "$REMOTE_SCRIPT" <<EOF
from faster_whisper import WhisperModel
import os


media_file="/content/$(basename "$target_media")"

output_srt="/content/$(basename "$target_srt")"

gen_txt = ${gen_txt}

output_txt = "/content/$(basename "$target_txt")" if gen_txt and "$target_txt" else ""



if not os.path.exists(media_file):

    raise FileNotFoundError(media_file)



print("Carregando Whisper...")


model = WhisperModel(
    "large-v3",
    device="cuda",
    compute_type="float16"
)



print("Transcrevendo...")


segments, info = model.transcribe(
    media_file,
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


srt_lines = []
txt_lines = []

for i, seg in enumerate(segments, 1):

    text = seg.text.strip()

    if text:

        srt_lines.append(
            f"{i}\\n"
            f"{tempo(seg.start)} --> {tempo(seg.end)}\\n"
            f"{text}\\n\\n"
        )

        txt_lines.append(text)


with open(output_srt, "w", encoding="utf-8") as f:

    f.writelines(srt_lines)


print("Criado:", output_srt)


if gen_txt and output_txt:

    with open(output_txt, "w", encoding="utf-8") as f:

        f.write("\\n".join(txt_lines) + "\\n")

    print("Criado:", output_txt)

EOF


    ok "Script remoto criado"

}

########################################
# Executar Whisper remoto
########################################

run_whisper() {


    info "Enviando script Whisper..."


    if ! colab upload \
        -s "$SESSION" \
        "$REMOTE_SCRIPT" \
        "/content/$REMOTE_SCRIPT"; then

        warn "Falha ao enviar script Whisper"

        return 1

    fi



    info "Rodando Whisper na T4..."


    if ! colab exec \
        -s "$SESSION" \
        -f "$REMOTE_SCRIPT" \
        --timeout 7200; then

        warn "Erro na execução do Whisper no Colab"

        return 1

    fi



    ok "Transcrição finalizada"

    return 0

}



########################################
# Baixar legenda
########################################

download_srt() {

    local target_srt="$1"


    info "Baixando legenda (.srt)..."


    if ! colab download \
        -s "$SESSION" \
        "/content/$(basename "$target_srt")" \
        "$target_srt"; then

        warn "Falha ao baixar legenda de $target_srt"

        return 1

    fi



    ok "Legenda salva: $target_srt"

    return 0

}



########################################
# Baixar texto (.txt)
########################################

download_txt() {

    local target_txt="$1"


    info "Baixando texto (.txt)..."


    if ! colab download \
        -s "$SESSION" \
        "/content/$(basename "$target_txt")" \
        "$target_txt"; then

        warn "Falha ao baixar texto de $target_txt"

        return 1

    fi



    ok "Texto salvo: $target_txt"

    return 0

}



########################################
# Limpeza remota
########################################

remote_cleanup() {

    local target_media="$1"
    local target_srt="$2"
    local target_txt="${3:-}"


    info "Limpando arquivos temporários no Colab..."


    cat > /tmp/cleanup.py <<EOF
import os

files = [
    "/content/$(basename "$target_media")",
    "/content/$(basename "$target_srt")",
    "/content/$REMOTE_SCRIPT"
]

if "$target_txt":
    files.append("/content/$(basename "$target_txt")")


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

    rm -f /tmp/cleanup.py
    rm -f "$REMOTE_SCRIPT" 2>/dev/null || true

}


########################################
# Processar arquivo individual
########################################

process_single_file() {

    local input_file="$1"
    local name="${input_file%.*}"
    local srt="${name}.srt"
    local txt="${name}.txt"
    local media_file=""
    local audio=""

    if [ -f "$srt" ] && { [ "$GENERATE_TXT" -eq 0 ] || [ -f "$txt" ]; }; then

        info "Saída(s) já existente(s) para $(basename "$input_file"). Pulando..."

        return 0

    fi

    if [ "$USE_VIDEO" -eq 1 ]; then

        media_file="$input_file"

        info "Modo vídeo: enviando $(basename "$input_file") diretamente..."

    else

        audio="${name}.opus"

        if ! prepare_audio "$input_file"; then

            warn "Falha ao preparar áudio para: $input_file. Pulando..."

            return 1

        fi

        media_file="$audio"

    fi

    if ! upload_parallel "$SESSION" "$media_file" "/content/$(basename "$media_file")"; then

        warn "Falha no upload para: $media_file. Pulando..."

        [ "$USE_VIDEO" -eq 0 ] && rm -f "$audio" 2>/dev/null || true

        return 1

    fi

    create_remote_script "$media_file" "$srt" "$txt" "$GENERATE_TXT"

    if ! run_whisper; then

        warn "Falha na transcrição de: $input_file. Pulando..."

        remote_cleanup "$media_file" "$srt" "$txt"

        [ "$USE_VIDEO" -eq 0 ] && rm -f "$audio" 2>/dev/null || true

        return 1

    fi

    if ! download_srt "$srt"; then

        warn "Falha ao baixar legenda para: $input_file."

    else

        ok "Legenda gerada com sucesso: $srt"

    fi

    if [ "$GENERATE_TXT" -eq 1 ]; then

        if ! download_txt "$txt"; then

            warn "Falha ao baixar texto para: $input_file."

        else

            ok "Texto gerado com sucesso: $txt"

        fi

    fi

    remote_cleanup "$media_file" "$srt" "$txt"

    if [ "$USE_VIDEO" -eq 0 ] && [ -f "$audio" ]; then

        rm -f "$audio"

    fi

    return 0

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

  $0 [-v|--video] [-t|--txt] [-d|--dir] <arquivo|diretorio>

Opções:

  -v, --video
      Envia o vídeo diretamente para o Whisper (ignora extração FFmpeg local)

  -t, --txt, --text
      Gera também um arquivo de texto simples (.txt) com a transcrição bruta

  -d, --dir <diretório>
      Processa todos os arquivos de mídia contidos no diretório especificado

Comandos:

  $0 status
      Mostra sessão atual

  $0 stop
      Encerra sessão Colab

Exemplos:

  $0 "Aula 07 - Analise.mp4"
  $0 -t -v "Aula 07 - Analise.mp4"
  $0 -t /caminho/para/pasta_videos
  $0 -v -t -d /caminho/para/pasta_videos

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


    USE_VIDEO=0
    GENERATE_TXT=0
    INPUT=""

    while [ $# -gt 0 ]; do

        case "$1" in

            -v|--video)

                USE_VIDEO=1

                shift

            ;;

            -t|--txt|--text)

                GENERATE_TXT=1

                shift

            ;;

            -d|--dir)

                shift

                if [ $# -gt 0 ]; then

                    INPUT="$1"

                    shift

                else

                    error "A opção -d/--dir requer um diretório como argumento"

                fi

            ;;

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

            *)

                if [ -z "$INPUT" ]; then

                    INPUT="$1"

                else

                    error "Opção desconhecida ou múltiplos caminhos fornecidos: $1"

                fi

                shift

            ;;

        esac

    done


    if [ -z "$INPUT" ]; then

        show_help

        exit 1

    fi


    FILES=()

    if [ -d "$INPUT" ]; then

        info "Diretório detectado: $INPUT"

        info "Buscando arquivos de mídia..."

        while IFS= read -r -d '' file; do

            FILES+=("$file")

        done < <(find "$INPUT" -maxdepth 1 -type f \( \
            -iname "*.mp4" -o \
            -iname "*.mkv" -o \
            -iname "*.mov" -o \
            -iname "*.avi" -o \
            -iname "*.ts"  -o \
            -iname "*.webm" -o \
            -iname "*.flv" -o \
            -iname "*.m4v" -o \
            -iname "*.mp3" -o \
            -iname "*.wav" -o \
            -iname "*.m4a" -o \
            -iname "*.opus" -o \
            -iname "*.aac" \
        \) -print0 | sort -z)

        TOTAL=${#FILES[@]}

        if [ "$TOTAL" -eq 0 ]; then

            error "Nenhum arquivo de mídia suportado encontrado em: $INPUT"

        fi

        info "Encontrado(s) $TOTAL arquivo(s) para processar."

    elif [ -f "$INPUT" ]; then

        FILES+=("$INPUT")

        TOTAL=1

    else

        error "Arquivo ou diretório não encontrado: $INPUT"

    fi


    check_dependencies


    cancel_timer


    ensure_session


    ensure_whisper


    SUCCESS_COUNT=0
    FAIL_COUNT=0

    for i in "${!FILES[@]}"; do

        CURRENT=$((i + 1))
        FILE="${FILES[$i]}"

        echo
        info "=================================================="
        info "[$CURRENT/$TOTAL] Processando: $(basename "$FILE")"
        info "=================================================="

        if process_single_file "$FILE"; then

            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))

        else

            FAIL_COUNT=$((FAIL_COUNT + 1))

            warn "Falha no arquivo: $FILE"

        fi

    done


    start_timer


    echo

    echo "======================================"

    ok "Processamento concluído!"

    echo "Sucessos: $SUCCESS_COUNT / $TOTAL"

    if [ "$FAIL_COUNT" -gt 0 ]; then

        warn "Falhas: $FAIL_COUNT / $TOTAL"

    fi

    echo

    echo "Sessão ficará ativa por 10 minutos."

    echo "Use:"

    echo "  $0 stop"

    echo

    echo "======================================"


}



main "$@"
