#!/usr/bin/env sh

set -u

# Batch image-to-image editing with xAI Grok Imagine through curl.
# Required: curl, python3, and XAI_API_KEY (or GROK_API_KEY/API_KEY).

CWD=$(pwd -P)

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$CWD" "$1" ;;
  esac
}

IMAGE_DIR=$(abs_path "${IMAGE_DIR:-$CWD/image}")
OUTPUT_DIR=$(abs_path "${OUTPUT_DIR:-$CWD/grok_image_output}")
LOG_DIR=$(abs_path "${LOG_DIR:-$OUTPUT_DIR/logs}")

BASE_URL=${BASE_URL:-https://api.x.ai}
MODEL=${MODEL:-grok-imagine-image-edit}
PROMPT=${PROMPT:-为这张黑白线稿上色，请严格保留原始的线条、构图和所有内容，不要做任何改动或重绘。}
NEGATIVE_PROMPT=${NEGATIVE_PROMPT:-不要出现黑白、灰度、模糊、低画质、变形、多余的文字或水印。}
RESPONSE_FORMAT=${RESPONSE_FORMAT:-}
CONNECT_TIMEOUT=${CONNECT_TIMEOUT:-30}
TIMEOUT=${TIMEOUT:-300}
DOWNLOAD_TIMEOUT=${DOWNLOAD_TIMEOUT:-180}
SEND_NEGATIVE_PROMPT_FIELD=${SEND_NEGATIVE_PROMPT_FIELD:-0}

API_KEY=${XAI_API_KEY:-${GROK_API_KEY:-${API_KEY:-}}}

usage() {
  cat <<EOF
用法:
  XAI_API_KEY="你的 key" $0 [选项]

选项:
  --base-url URL       自定义 API base，默认: https://api.x.ai
                       支持传入 https://host、https://host/v1 或完整 /v1/images/edits
  --image-dir DIR      自定义输入图片目录，默认: 当前路径/image
  --output-dir DIR     自定义输出目录，默认: 当前路径/grok_image_output
  --model MODEL        自定义模型，默认: grok-imagine-image-edit
  --help               显示帮助

也可以用环境变量:
  BASE_URL、IMAGE_DIR、OUTPUT_DIR、MODEL、PROMPT、NEGATIVE_PROMPT
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-url)
      [ "$#" -ge 2 ] || { printf '缺少 --base-url 的值\n' >&2; exit 1; }
      BASE_URL=$2
      shift 2
      ;;
    --base-url=*)
      BASE_URL=${1#*=}
      shift
      ;;
    --image-dir)
      [ "$#" -ge 2 ] || { printf '缺少 --image-dir 的值\n' >&2; exit 1; }
      IMAGE_DIR=$(abs_path "$2")
      shift 2
      ;;
    --image-dir=*)
      IMAGE_DIR=$(abs_path "${1#*=}")
      shift
      ;;
    --output-dir)
      [ "$#" -ge 2 ] || { printf '缺少 --output-dir 的值\n' >&2; exit 1; }
      OUTPUT_DIR=$(abs_path "$2")
      LOG_DIR=$(abs_path "$OUTPUT_DIR/logs")
      shift 2
      ;;
    --output-dir=*)
      OUTPUT_DIR=$(abs_path "${1#*=}")
      LOG_DIR=$(abs_path "$OUTPUT_DIR/logs")
      shift
      ;;
    --model)
      [ "$#" -ge 2 ] || { printf '缺少 --model 的值\n' >&2; exit 1; }
      MODEL=$2
      shift 2
      ;;
    --model=*)
      MODEL=${1#*=}
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '未知选项: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

RUN_ID=$(date '+%Y%m%d_%H%M%S')
LOG_FILE="$LOG_DIR/run_$RUN_ID.log"
SUCCESS_LOG="$LOG_DIR/success_$RUN_ID.tsv"
FAIL_LOG="$LOG_DIR/failure_$RUN_ID.tsv"

log() {
  mkdir -p "$LOG_DIR"
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"
}

one_line() {
  printf '%s' "$1" | tr '\t\r\n' '   '
}

mime_type() {
  lower=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    *.jpg|*.jpeg) printf 'image/jpeg\n' ;;
    *.png) printf 'image/png\n' ;;
    *.webp) printf 'image/webp\n' ;;
    *.gif) printf 'image/gif\n' ;;
    *.bmp) printf 'image/bmp\n' ;;
    *) printf 'image/png\n' ;;
  esac
}

api_url() {
  base=${BASE_URL%/}
  case "$base" in
    */v1/images/edits) printf '%s\n' "$base" ;;
    */v1) printf '%s/images/edits\n' "$base" ;;
    *) printf '%s/v1/images/edits\n' "$base" ;;
  esac
}

fail_item() {
  src=$1
  stage=$2
  detail=$3
  failures=$((failures + 1))
  log "失败: $src | $stage | $(one_line "$detail")"
  printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$src" "$stage" "$(one_line "$detail")" >> "$FAIL_LOG"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '缺少命令: %s\n' "$1" >&2
    exit 1
  fi
}

require_command curl
require_command python3

if [ -z "$API_KEY" ]; then
  printf '请先设置 XAI_API_KEY，例如：export XAI_API_KEY="你的 key"\n' >&2
  exit 1
fi

if [ ! -d "$IMAGE_DIR" ]; then
  printf '图片文件夹不存在: %s\n' "$IMAGE_DIR" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

TMP_ROOT=${TMPDIR:-/tmp}
TMP_DIR=$(mktemp -d "$TMP_ROOT/grok_image_edit.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

LIST_FILE="$TMP_DIR/images.list"
find "$IMAGE_DIR" -type f \( \
  -iname '*.png' -o \
  -iname '*.jpg' -o \
  -iname '*.jpeg' -o \
  -iname '*.webp' -o \
  -iname '*.gif' -o \
  -iname '*.bmp' \
\) -print | sort > "$LIST_FILE"

total=$(wc -l < "$LIST_FILE" | tr -d ' ')
if [ "$total" = "0" ]; then
  log "未找到图片: $IMAGE_DIR"
  exit 0
fi

printf 'time\tsource\toutput\tmodel\n' > "$SUCCESS_LOG"
printf 'time\tsource\tstage\tdetail\n' > "$FAIL_LOG"

FULL_PROMPT=$PROMPT
if [ -n "$NEGATIVE_PROMPT" ]; then
  FULL_PROMPT="$PROMPT

反向提示词：$NEGATIVE_PROMPT"
fi

ENDPOINT=$(api_url)
log "输入目录: $IMAGE_DIR"
log "输出目录: $OUTPUT_DIR"
log "接口地址: $ENDPOINT"
log "模型: $MODEL"
log "共找到 $total 张图片"

successes=0
failures=0
index=0

while IFS= read -r image_path; do
  index=$((index + 1))
  image_mime=$(mime_type "$image_path")
  base_name=$(basename "$image_path")
  stem=${base_name%.*}
  out_stem="$OUTPUT_DIR/${index}_${stem}_grok"
  request_json="$TMP_DIR/request_$index.json"
  response_json="$TMP_DIR/response_$index.json"
  curl_err="$TMP_DIR/curl_$index.err"
  url_file="$TMP_DIR/url_$index.txt"

  log "处理中 ($index/$total): $image_path"

  if ! IMAGE_PATH="$image_path" \
    IMAGE_MIME="$image_mime" \
    MODEL="$MODEL" \
    FULL_PROMPT="$FULL_PROMPT" \
    NEGATIVE_PROMPT="$NEGATIVE_PROMPT" \
    RESPONSE_FORMAT="$RESPONSE_FORMAT" \
    SEND_NEGATIVE_PROMPT_FIELD="$SEND_NEGATIVE_PROMPT_FIELD" \
    python3 - > "$request_json" <<'PY'
import base64
import json
import os

with open(os.environ["IMAGE_PATH"], "rb") as f:
    encoded = base64.b64encode(f.read()).decode("ascii")

payload = {
    "model": os.environ["MODEL"],
    "prompt": os.environ["FULL_PROMPT"],
    "image": {
        "url": f"data:{os.environ['IMAGE_MIME']};base64,{encoded}",
        "type": "image_url",
    },
}

response_format = os.environ.get("RESPONSE_FORMAT", "").strip()
if response_format:
    payload["response_format"] = response_format

if os.environ.get("SEND_NEGATIVE_PROMPT_FIELD") == "1":
    payload["negative_prompt"] = os.environ.get("NEGATIVE_PROMPT", "")

print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
PY
  then
    fail_item "$image_path" "build_request" "生成 JSON 请求失败"
    continue
  fi

  curl_status=0
  http_code=$(curl -sS \
    --connect-timeout "$CONNECT_TIMEOUT" \
    --max-time "$TIMEOUT" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $API_KEY" \
    -o "$response_json" \
    -w '%{http_code}' \
    -d @"$request_json" \
    "$ENDPOINT" 2>"$curl_err") || curl_status=$?

  if [ "$curl_status" -ne 0 ]; then
    fail_item "$image_path" "curl" "$(cat "$curl_err")"
    continue
  fi

  if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
    preview=$(python3 - "$response_json" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        text = error.get("message") or json.dumps(error, ensure_ascii=False)
    elif error:
        text = str(error)
    elif isinstance(data, dict) and data.get("message"):
        text = str(data["message"])
    else:
        text = json.dumps(data, ensure_ascii=False)
except Exception:
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
print(re.sub(r"\s+", " ", text).strip()[:800])
PY
)
    fail_item "$image_path" "http_$http_code" "$preview"
    continue
  fi

  parse_result=$(python3 - "$response_json" "$out_stem" "$url_file" <<'PY'
import base64
import json
import os
import re
import sys
from urllib.parse import urlparse

response_path, out_stem, url_file = sys.argv[1:4]

def emit(kind, *parts):
    print("\t".join([kind, *[str(p) for p in parts]]))

def ext_from_mime(mime):
    mime = (mime or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }.get(mime, "")

def ext_from_url(url):
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""

def decode_data_uri(value, fallback_mime):
    text = str(value or "")
    mime = fallback_mime or ""
    raw = text
    match = re.match(r"^data:([^;,]+);base64,(.*)$", text, flags=re.S)
    if match:
        mime = match.group(1)
        raw = match.group(2)
    return base64.b64decode(raw, validate=False), mime

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)

try:
    with open(response_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
except Exception as exc:
    emit("error", f"接口返回非 JSON 内容: {exc}")
    raise SystemExit

if isinstance(data, dict) and data.get("error"):
    emit("error", json.dumps(data.get("error"), ensure_ascii=False))
    raise SystemExit

root = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else data
items = list(walk(root))

for item in items:
    if not isinstance(item, dict):
        continue
    mime = str(item.get("mime_type") or item.get("mime") or "")
    for key in ("b64_json", "base64", "data", "image"):
        value = item.get(key)
        if isinstance(value, str) and (value.startswith("data:image/") or len(value) > 300):
            try:
                content, resolved_mime = decode_data_uri(value, mime)
            except Exception as exc:
                emit("error", f"base64 解码失败: {exc}")
                raise SystemExit
            ext = ext_from_mime(resolved_mime) or ".png"
            out_path = out_stem + ext
            with open(out_path, "wb") as f:
                f.write(content)
            emit("file", out_path)
            raise SystemExit

for item in items:
    if not isinstance(item, dict):
        continue
    mime = str(item.get("mime_type") or item.get("mime") or "")
    for key in ("url", "image_url", "output"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("url")
        if not isinstance(value, str):
            continue
        if value.startswith("data:image/"):
            try:
                content, resolved_mime = decode_data_uri(value, mime)
            except Exception as exc:
                emit("error", f"data URI 解码失败: {exc}")
                raise SystemExit
            ext = ext_from_mime(resolved_mime) or ".png"
            out_path = out_stem + ext
            with open(out_path, "wb") as f:
                f.write(content)
            emit("file", out_path)
            raise SystemExit
        if value.startswith(("http://", "https://")):
            ext = ext_from_mime(mime) or ext_from_url(value) or ".jpg"
            with open(url_file, "w", encoding="utf-8") as f:
                f.write(value)
            emit("url", value, ext)
            raise SystemExit

preview = json.dumps(data, ensure_ascii=False)[:800]
emit("error", f"未识别到图片字段: {preview}")
PY
)

  result_kind=$(printf '%s\n' "$parse_result" | cut -f1)

  if [ "$result_kind" = "file" ]; then
    out_file=$(printf '%s\n' "$parse_result" | cut -f2-)
    if [ -s "$out_file" ]; then
      successes=$((successes + 1))
      log "成功: $image_path -> $out_file"
      printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$image_path" "$out_file" "$MODEL" >> "$SUCCESS_LOG"
    else
      fail_item "$image_path" "save_file" "解析到文件路径但文件为空: $out_file"
    fi
    continue
  fi

  if [ "$result_kind" = "url" ]; then
    generated_url=$(sed -n '1p' "$url_file")
    generated_ext=$(printf '%s\n' "$parse_result" | cut -f3)
    out_file="${out_stem}${generated_ext:-.jpg}"
    download_status=0
    download_code=$(curl -L -sS \
      --connect-timeout "$CONNECT_TIMEOUT" \
      --max-time "$DOWNLOAD_TIMEOUT" \
      -o "$out_file" \
      -w '%{http_code}' \
      "$generated_url" 2>"$curl_err") || download_status=$?

    if [ "$download_status" -ne 0 ]; then
      rm -f "$out_file"
      fail_item "$image_path" "download" "$(cat "$curl_err")"
      continue
    fi

    if [ "$download_code" -lt 200 ] || [ "$download_code" -ge 400 ] || [ ! -s "$out_file" ]; then
      rm -f "$out_file"
      fail_item "$image_path" "download_http_$download_code" "$generated_url"
      continue
    fi

    successes=$((successes + 1))
    log "成功: $image_path -> $out_file"
    printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$image_path" "$out_file" "$MODEL" >> "$SUCCESS_LOG"
    continue
  fi

  fail_detail=$(printf '%s\n' "$parse_result" | cut -f2-)
  fail_item "$image_path" "parse_response" "$fail_detail"
done < "$LIST_FILE"

log "完成: 成功 $successes / 失败 $failures / 总计 $total"
log "成功日志: $SUCCESS_LOG"
log "失败日志: $FAIL_LOG"
