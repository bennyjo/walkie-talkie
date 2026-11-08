#!/bin/bash
# Downloads the three speech models, checks each against its pinned SHA-256, and unpacks them.
# Takes about 1 GB, and needs about 2 GB free while downloading. Models go to $WALKIE_TALKIE_MODELS, by default ~/.local/share/walkie-talkie/models.
set -euo pipefail

MODELS="${WALKIE_TALKIE_MODELS:-$HOME/.local/share/walkie-talkie/models}"
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download
mkdir -p "$MODELS"

fetch() { # url sha256 file
  local url=$1 sha=$2 file=$3
  if [ ! -f "$MODELS/$file" ] || ! echo "$sha  $MODELS/$file" | shasum -a 256 -c --status; then
    echo "Downloading $file"
    curl -fL --retry 3 -o "$MODELS/$file.part" "$url"
    if ! echo "$sha  $MODELS/$file.part" | shasum -a 256 -c --status; then
      echo "Checksum mismatch for $file. Nothing was installed." >&2
      exit 1
    fi
    mv "$MODELS/$file.part" "$MODELS/$file"
  fi
}

unpack() { # archive folder
  if [ ! -d "$MODELS/$2" ]; then
    echo "Unpacking $1"
    tar xjf "$MODELS/$1" -C "$MODELS"
  fi
  rm -f "${MODELS:?}/${1:?}"
}

fetch "$BASE/asr-models/silero_vad.onnx" \
  9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6 silero_vad.onnx

PARAKEET=sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
if [ ! -d "$MODELS/$PARAKEET" ]; then
  fetch "$BASE/asr-models/$PARAKEET.tar.bz2" \
    5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf "$PARAKEET.tar.bz2"
  unpack "$PARAKEET.tar.bz2" "$PARAKEET"
fi

KOKORO=kokoro-en-v0_19
if [ ! -d "$MODELS/$KOKORO" ]; then
  fetch "$BASE/tts-models/$KOKORO.tar.bz2" \
    912804855a04745fa77a30be545b3f9a5d15c4d66db00b88cbcd4921df605ac7 "$KOKORO.tar.bz2"
  unpack "$KOKORO.tar.bz2" "$KOKORO"
fi

echo "Models are ready in $MODELS"
