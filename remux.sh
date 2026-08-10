#!/usr/bin/env bash

set -euo pipefail

########################################
# Configuração
########################################

CACHE_DIR="$HOME/.cache/remux_colab"
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
        || error "Colab CLI não encontrado em PATH. Instale ou verifique ~/.local/bin/colab"

    command -v ffprobe >/dev/null \
        || error "ffprobe não encontrado"

    command -v ffmpeg >/dev/null \
        || error "ffmpeg não encontrado"
}

########################################
# Sessão Colab
########################################

get_session() {
    if [ -f "$SESSION_FILE" ]; then
        SESSION=$(cat "$SESSION_FILE")
        if colab sessions 2>/dev/null | grep -q "$SESSION"; then
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
# Timer de desligamento automático
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
            info "10 minutos sem uso. Encerrando sessão Colab $SESSION..."
            colab stop -s "$SESSION" || true
            clear_session
        fi
    ) 200>&- >/dev/null 2>&1 &
    echo $! > "$TIMER_FILE"
}

ensure_session() {
    SESSION=$(get_session)
    if [ -n "$SESSION" ]; then
        ok "Usando sessão Colab existente: $SESSION"
        return
    fi

    info "Criando nova sessão Colab T4 GPU..."
    OUTPUT=$(colab new --gpu T4 2>&1 || true)
    SESSION=$(echo "$OUTPUT" | grep "Creating session" | sed -E "s/.*'([^']+)'.*/\1/")

    if [ -z "$SESSION" ]; then
        error "Não foi possível criar sessão no Colab. Verifique 'colab status' ou credenciais."
    fi

    save_session "$SESSION"
    ok "Sessão criada com sucesso: $SESSION"
}

########################################
# Análise de Mídia Local (ffprobe)
########################################

# Retorna JSON com os streams do arquivo
probe_file() {
    local file="$1"
    ffprobe -v error -show_streams -show_format -print_format json "$file" 2>/dev/null
}

# Retorna 0 se o vídeo precisa de transcode remoto, 1 se o vídeo é 100% seguro (copy)
check_video_needs_transcode() {
    local file="$1"
    local json
    json=$(probe_file "$file") || return 0

    python3 - <<EOF
import sys, json

try:
    data = json.loads('''$json''')
except Exception:
    sys.exit(0) # Se falhar o parse, transcodifica por segurança

video_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0]

if not video_streams:
    sys.exit(0)

v = video_streams[0]

codec = (v.get("codec_name") or "").lower()
profile = (v.get("profile") or "").lower()
level = int(v.get("level") or 0)
pix_fmt = (v.get("pix_fmt") or "").lower()
width = int(v.get("width") or 0)
height = int(v.get("height") or 0)
bits = 8
if "10" in pix_fmt or "12" in pix_fmt or v.get("bits_per_raw_sample") in ["10", "12"]:
    bits = 10

# FPS parse
avg_fps_str = v.get("avg_frame_rate", "0/1")
fps = 0.0
try:
    num, den = avg_fps_str.split("/")
    if float(den) > 0:
        fps = float(num) / float(den)
except Exception:
    fps = 0.0

# Regras Samsung Plasma PL51F4000
# Codec: H.264
if codec not in ["h264", "avc"]:
    sys.exit(0) # Precisa transcodificar

# Profile: High / Main / Baseline
if profile and ("high 10" in profile or "4:2:2" in profile or "4:4:4" in profile):
    sys.exit(0)

# Level <= 41 (4.1)
if level > 41:
    sys.exit(0)

# Pixel Format: yuv420p & 8-bit
if pix_fmt != "yuv420p" or bits > 8:
    sys.exit(0)

# Resolução <= 1920x1080
if width > 1920 or height > 1080:
    sys.exit(0)

# FPS <= 30 (com margem de tolerância até 30.5)
if fps > 30.5:
    sys.exit(0)

# Vídeo é totalmente compatível!
sys.exit(1)
EOF
}

# Retorna 0 se algum áudio precisa de conversão, 1 se todos os áudios são compatíveis (copy)
check_audio_needs_transcode() {
    local file="$1"
    local json
    json=$(probe_file "$file") || return 0

    python3 - <<EOF
import sys, json

try:
    data = json.loads('''$json''')
except Exception:
    sys.exit(0)

audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
if not audio_streams:
    sys.exit(1) # Sem áudio, não precisa converter áudio

SAFE_AUDIO_CODECS = {"ac3", "aac", "mp3"}

for a in audio_streams:
    codec = (a.get("codec_name") or "").lower()
    # E-AC-3, DTS, TrueHD, FLAC, Opus, etc MUST be converted
    if codec not in SAFE_AUDIO_CODECS:
        sys.exit(0) # Pelo menos 1 áudio precisa de conversão

sys.exit(1)
EOF
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
print(os.path.exists("""$remote_path"""))
EOF

    local exists_result
    exists_result=$(colab exec -s "$session" -f /tmp/check_remote_file.py --timeout 60 2>/dev/null || true)
    rm -f /tmp/check_remote_file.py

    if echo "$exists_result" | grep -q "True"; then
        info "Arquivo já existe no Colab, reutilizando..."
        return 0
    fi

    info "Iniciando upload em chunks paralelos..."

    local tmp_dir
    tmp_dir=$(mktemp -d)
    trap 'rm -rf "$tmp_dir"' EXIT INT TERM

    split -b 50M "$local_file" "$tmp_dir/chunk_"

    local chunks=("$tmp_dir"/chunk_*)
    local num_chunks=${#chunks[@]}
    local max_jobs=10

    info "Total de chunks: $num_chunks (até $max_jobs uploads simultâneos)"

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
import glob, os, subprocess

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

    ok "Upload para Colab concluído com sucesso!"
    return 0
}

setup_rclone_colab() {
    local session="$1"
    info "Configurando rclone no Colab..."
    colab upload -s "$session" "$HOME/.config/rclone/rclone.conf" "/content/rclone.conf" >/dev/null 2>&1 || true
    cat > /tmp/setup_rclone.py <<EOF
import subprocess, shutil
if not shutil.which("rclone"):
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "rclone"], check=True)
print("RCLONE_OK")
EOF
    colab exec -s "$session" -f /tmp/setup_rclone.py --timeout 120 >/dev/null 2>&1 || true
    rm -f /tmp/setup_rclone.py
}

download_gdrive() {
    local session="$1"
    local remote_file="$2"
    local local_file="$3"
    local filename
    filename=$(basename "$remote_file")

    setup_rclone_colab "$session"

    info "Enviando arquivo processado do Colab para Google Drive..."
    python3 -c '
import sys, json
rf = sys.argv[1]
code = f"""import subprocess
cmd = ["rclone", "copy", {json.dumps(rf)}, "sam:remux_temp/", "--config", "/content/rclone.conf", "--drive-chunk-size", "128M"]
subprocess.run(cmd, check=True)
print("PUSH_OK")
"""
with open("/tmp/colab_push.py", "w") as f:
    f.write(code)
' "$remote_file"

    local push_res
    push_res=$(colab exec -s "$session" -f /tmp/colab_push.py --timeout 600 2>&1 || true)
    rm -f /tmp/colab_push.py

    if ! echo "$push_res" | grep -q "PUSH_OK"; then
        echo "$push_res"
        warn "Falha ao enviar arquivo do Colab para Google Drive"
        return 1
    fi

    info "Baixando $filename do Google Drive via rclone..."
    local local_dir
    local_dir=$(dirname "$local_file")
    mkdir -p "$local_dir"

    if ! rclone copy "sam:remux_temp/$filename" "$local_dir/" --drive-chunk-size 64M --buffer-size 64M --vfs-read-chunk-size 64M --progress; then
        warn "Falha no rclone download do Google Drive"
        return 1
    fi

    local downloaded_local="$local_dir/$filename"
    if [ "$downloaded_local" != "$local_file" ]; then
        mv "$downloaded_local" "$local_file"
    fi

    rclone delete "sam:remux_temp/$filename" >/dev/null 2>&1 || true
    ok "Download concluído via Google Drive!"
    return 0
}

########################################
# Gerar Script Remoto de Transcoding (Samsung TV Spec)
########################################

create_remote_transcode_script() {
    local remote_input="$1"
    local remote_output="$2"
    local force_cpu="${3:-0}"
    local best_quality="${4:-0}"

    cat > /tmp/remote_transcode.py <<EOF
import subprocess
import json
import os
import sys

input_file = """$remote_input"""
output_file = """$remote_output"""
force_cpu = ${force_cpu}
best_quality = ${best_quality}

if not os.path.exists(input_file):
    print(f"Erro: arquivo de entrada não existe: {input_file}")
    sys.exit(1)

# Inspect streams via ffprobe
cmd_probe = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-print_format", "json", input_file]
res = subprocess.run(cmd_probe, capture_output=True, text=True)
data = json.loads(res.stdout) if res.returncode == 0 else {"streams": []}

streams = data.get("streams", [])

v_streams = [s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0]
a_streams = [s for s in streams if s.get("codec_type") == "audio"]

# Check video compatibility
needs_v_transcode = False
width = 0
height = 0
fps = 0.0

if v_streams:
    v0 = v_streams[0]
    codec = (v0.get("codec_name") or "").lower()
    profile = (v0.get("profile") or "").lower()
    level = int(v0.get("level") or 0)
    pix_fmt = (v0.get("pix_fmt") or "").lower()
    width = int(v0.get("width") or 0)
    height = int(v0.get("height") or 0)
    
    avg_fps_str = v0.get("avg_frame_rate", "0/1")
    try:
        n, d = avg_fps_str.split("/")
        if float(d) > 0:
            fps = float(n) / float(d)
    except:
        fps = 0.0

    if codec not in ["h264", "avc"] or level > 41 or pix_fmt != "yuv420p" or width > 1920 or height > 1080 or fps > 30.5:
        needs_v_transcode = True

# Build FFmpeg command (map all video, audio, and subtitle streams)
cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-i", input_file, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]

# Video options
if needs_v_transcode:
    # Check GPU NVENC availability
    use_gpu = False
    if not force_cpu:
        chk_gpu = subprocess.run(["ffmpeg", "-h", "encoder=h264_nvenc"], capture_output=True)
        if chk_gpu.returncode == 0:
            use_gpu = True

    vf_filters = []

    # Downscale if > 1080p
    if width > 1920 or height > 1080:
        vf_filters.append("scale='min(1920,iw)':min(1080,ih)':force_original_aspect_ratio=decrease")

    if use_gpu:
        print("Usando GPU NVENC (h264_nvenc)...")
        cmd.extend(["-c:v", "h264_nvenc", "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p"])
        if best_quality:
            cmd.extend(["-preset", "p7", "-rc", "vbr", "-cq", "18"])
        else:
            cmd.extend(["-preset", "p4", "-rc", "vbr", "-cq", "22"])
    else:
        print("Usando CPU (libx264)...")
        cmd.extend(["-c:v", "libx264", "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p"])
        if best_quality:
            cmd.extend(["-preset", "slow", "-crf", "18"])
        else:
            cmd.extend(["-preset", "medium", "-crf", "22"])

    if vf_filters:
        cmd.extend(["-vf", ",".join(vf_filters)])

    # Cap FPS to 30 if > 30
    if fps > 30.5:
        cmd.extend(["-r", "30"])

else:
    print("Vídeo seguro para a TV Samsung PL51F4000AG -> -c:v copy")
    cmd.extend(["-c:v", "copy"])

# Audio options
SAFE_AUDIO = {"ac3", "aac", "mp3"}

for idx, a in enumerate(a_streams):
    a_codec = (a.get("codec_name") or "").lower()
    ch = int(a.get("channels") or 2)
    
    if a_codec in SAFE_AUDIO:
        print(f"Áudio #{idx} ({a_codec}): compatível -> -c:a:{idx} copy")
        cmd.extend([f"-c:a:{idx}", "copy"])
    else:
        print(f"Áudio #{idx} ({a_codec}): incompatível (E-AC-3/DTS/etc) -> convertendo para AC-3 48kHz 384k ({'5.1' if ch >= 6 else 'Stereo'})")
        cmd.extend([
            f"-c:a:{idx}", "ac3",
            f"-ar:a:{idx}", "48000",
            f"-b:a:{idx}", "384k"
        ])
        if ch >= 6:
            cmd.extend([f"-ac:a:{idx}", "6"])
        else:
            cmd.extend([f"-ac:a:{idx}", "2"])

# Copy text subtitles
cmd.extend(["-c:s", "copy"])

# Output format MKV
cmd.extend(["-f", "matroska", output_file])

print("Executando FFmpeg:", " ".join(cmd))
sys.stdout.flush()

proc = subprocess.run(cmd)
if proc.returncode != 0:
    print("FFmpeg falhou!")
    sys.exit(1)

print("TRANSCODE_SUCCESS")
EOF
}

########################################
# Processar arquivo individual
########################################

process_single_file() {
    exec 200>"$CACHE_DIR/remux.lock"
    flock -x 200

    local input_file="$1"
    local force_remote="${FORCE_REMOTE:-0}"
    local overwrite="${OVERWRITE:-0}"
    local best_quality="${BEST_QUALITY:-0}"
    local force_cpu="${FORCE_CPU:-0}"

    local dir
    dir=$(dirname "$input_file")
    local filename
    filename=$(basename "$input_file")
    local name="${filename%.*}"

    local output_file
    if [ "$overwrite" -eq 1 ]; then
        output_file="${dir}/${name}.mkv"
    else
        output_file="${dir}/${name}.remux.mkv"
    fi

    if [ -f "$output_file" ] && [ "$overwrite" -eq 0 ]; then
        info "Arquivo de saída já existe: $(basename "$output_file"). Pulando..."
        return 0
    fi

    info "Analisando arquivo local: $filename"

    local v_needs=0
    local a_needs=0

    if check_video_needs_transcode "$input_file"; then
        v_needs=1
    fi

    if check_audio_needs_transcode "$input_file"; then
        a_needs=1
    fi

    # Se nada precisa de transcode e não for forçado o modo remoto:
    if [ "$v_needs" -eq 0 ] && [ "$a_needs" -eq 0 ] && [ "$force_remote" -eq 0 ]; then
        ok "Arquivo 100% compatível com a TV Samsung Plasma PL51F4000AG!"
        info "Realizando remux local super rápido para MKV..."

        if ffmpeg -y -loglevel error -i "$input_file" -map 0 -c copy -f matroska "$output_file"; then
            ok "Remux local concluído: $output_file"
            return 0
        else
            warn "Remux local falhou. Tentando modo remoto no Colab..."
        fi
    else
        if [ "$v_needs" -eq 1 ]; then
            info "Vídeo precisa de transcodificação (H.264 High@4.0 8-bit yuv420p ≤1080p ≤30fps)."
        fi
        if [ "$a_needs" -eq 1 ]; then
            info "Áudio (E-AC-3/DTS/etc) precisa de conversão para AC-3 48kHz 384k."
        fi
    fi

    # Processamento Remoto no Colab T4 GPU
    ensure_session

    local remote_input="/content/input_$(basename "$input_file")"
    local remote_output="/content/output_${name}.mkv"

    if ! upload_parallel "$SESSION" "$input_file" "$remote_input"; then
        warn "Falha ao enviar arquivo para o Colab"
        return 1
    fi

    create_remote_transcode_script "$remote_input" "$remote_output" "$force_cpu" "$best_quality"

    info "Executando transcoder na T4 GPU do Colab..."
    local run_res
    run_res=$(colab exec -s "$SESSION" -f /tmp/remote_transcode.py --timeout 14400 2>&1 || true)
    rm -f /tmp/remote_transcode.py

    if ! echo "$run_res" | grep -q "TRANSCODE_SUCCESS"; then
        echo "$run_res"
        warn "Erro ou falha na execução do FFmpeg no Colab"
        return 1
    fi

    ok "Transcodificação concluída na GPU Colab!"

    info "Baixando arquivo processado via Google Drive..."
    if ! download_gdrive "$SESSION" "$remote_output" "$output_file"; then
        warn "Falha ao baixar arquivo transcodificado de $remote_output"
        return 1
    fi

    ok "Arquivo salvo localmente: $output_file"

    info "Limpando arquivos temporários no Colab..."
    cat > /tmp/remote_cleanup.py <<EOF
import os
for f in ["$remote_input", "$remote_output"]:
    if os.path.exists(f):
        os.remove(f)
print("clean")
EOF
    colab exec -s "$SESSION" -f /tmp/remote_cleanup.py --timeout 60 >/dev/null 2>&1 || true
    rm -f /tmp/remote_cleanup.py

    if [ "$overwrite" -eq 1 ] && [ "$input_file" != "$output_file" ]; then
        rm -f "$input_file"
        ok "Arquivo original substituído."
    fi

    flock -u 200 2>/dev/null || true
    exec 200>&-
    return 0
}

########################################
# Ajuda
########################################

show_help() {
cat <<EOF

Uso:
  $0 [opções] <arquivo|diretorio>
  $0 status
  $0 stop

Descrição:
  Inspecciona e remuxa/transcodifica vídeos para total compatibilidade com a 
  TV Samsung Plasma PL51F4000AG (H.264 High@4.0 8-bit yuv420p <=1080p <=30fps, 
  áudio AC-3/AAC/MP3, converte E-AC-3/DTS/Opus -> AC-3 48kHz 384k) utilizando 
  aceleração de GPU NVENC no Google Colab T4.

Opções:
  -d, --dir <diretório>     Processa todos os arquivos de vídeo em um diretório.
  -r, --remote              Força transcodificação remota no Colab (ignora verificação local).
  -q, --quality, --best     Ativa modo alta qualidade (NVENC preset p7, CQ 18 / CRF 18).
  --cpu                     Força uso de CPU (libx264) no Colab em vez da GPU NVENC.
  -w, --overwrite           Substitui o arquivo original após a conversão.
  -h, --help                Exibe esta mensagem de ajuda.

Comandos:
  status                    Exibe status da sessão ativa no Google Colab.
  stop                      Encerra a sessão ativa no Google Colab.

Exemplos:
  $0 "Filme.mkv"
  $0 -q "Filme 4K.mkv"
  $0 -d /caminho/para/pasta_videos
  $0 stop

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

    FORCE_REMOTE=0
    BEST_QUALITY=0
    FORCE_CPU=0
    OVERWRITE=0
    INPUT=""

    while [ $# -gt 0 ]; do
        case "$1" in
            -r|--remote)
                FORCE_REMOTE=1
                shift
            ;;
            -q|--quality|--best)
                BEST_QUALITY=1
                shift
            ;;
            --cpu)
                FORCE_CPU=1
                shift
            ;;
            -w|--overwrite)
                OVERWRITE=1
                shift
            ;;
            -d|--dir)
                shift
                if [ $# -gt 0 ]; then
                    INPUT="$1"
                    shift
                else
                    error "A opção -d/--dir requer um caminho de diretório"
                fi
            ;;
            status)
                if [ -f "$SESSION_FILE" ]; then
                    SESSION=$(cat "$SESSION_FILE")
                    echo "Sessão Colab salva: $SESSION"
                    colab status -s "$SESSION" || true
                else
                    warn "Nenhuma sessão ativa salva"
                fi
                exit 0
            ;;
            stop)
                cancel_timer
                if [ -f "$SESSION_FILE" ]; then
                    SESSION=$(cat "$SESSION_FILE")
                    info "Encerrando sessão Colab $SESSION..."
                    colab stop -s "$SESSION" || true
                    clear_session
                    ok "Sessão encerrada"
                else
                    warn "Nenhuma sessão salva para encerrar"
                fi
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
                    error "Opção ou caminho desconhecido: $1"
                fi
                shift
            ;;
        esac
    done

    if [ -z "$INPUT" ]; then
        show_help
        exit 1
    fi

    check_dependencies
    cancel_timer

    FILES=()
    if [ -d "$INPUT" ]; then
        info "Buscando arquivos de vídeo em: $INPUT"
        while IFS= read -r -d '' file; do
            FILES+=("$file")
        done < <(find "$INPUT" -maxdepth 1 -type f \( \
            -iname "*.mp4" -o \
            -iname "*.mkv" -o \
            -iname "*.mov" -o \
            -iname "*.avi" -o \
            -iname "*.ts"  -o \
            -iname "*.webm" -o \
            -iname "*.m4v" \
        \) ! -name "*.remux.mkv" -print0 | sort -z)

        TOTAL=${#FILES[@]}
        if [ "$TOTAL" -eq 0 ]; then
            error "Nenhum arquivo de vídeo suportado encontrado em: $INPUT"
        fi
        info "Encontrado(s) $TOTAL arquivo(s) para processamento."
    elif [ -f "$INPUT" ]; then
        FILES+=("$INPUT")
        TOTAL=1
    else
        error "Arquivo ou diretório não encontrado: $INPUT"
    fi

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
            warn "Falha ao processar: $FILE"
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
    echo "Sessão Colab permanecerá ativa por 10 minutos para novos trabalhos."
    echo "Para encerrar agora, execute:"
    echo "  $0 stop"
    echo "======================================"
}

main "$@"
