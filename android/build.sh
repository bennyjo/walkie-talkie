#!/bin/bash
# Builds the app with no Gradle and no system-wide install. The first run downloads a JDK and
# Android's build tools into android/.toolchain/ (about 570 MB), each checked against a pinned
# checksum. Delete that folder whenever you like.
#
#   ./build.sh                 the phone's APK, out/walkie-talkie.apk
#   ./build.sh --debug         the emulator's test APK, out/walkie-talkie-debug.apk, under its own
#                              package name, so it can't install over the phone's APK
#
# Set WALKIE_KEYSTORE to keep one signing key across builds, so an update installs over the last
# version. Without it, the key is made in android/.toolchain/ and goes when that folder goes.
set -euo pipefail

cd "$(dirname "$0")"
VARIANT=release
OUT_NAME=walkie-talkie.apk
DEBUG_FLAG=()
if [ "${1:-}" = "--debug" ]; then
  VARIANT=debug
  OUT_NAME=walkie-talkie-debug.apk
  DEBUG_FLAG=(--debug-mode --rename-manifest-package dev.walkietalkie.test)
fi

TOOLS="${WALKIE_ANDROID_TOOLCHAIN:-$PWD/.toolchain}"
GOOGLE=https://dl.google.com/android/repository
TEMURIN=https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9

fetch() { # url sha-algorithm checksum file
  local url=$1 algorithm=$2 sum=$3 file=$4
  if [ ! -f "$TOOLS/$file" ]; then
    echo "Downloading $file"
    curl -fsSL --retry 3 -o "$TOOLS/$file.part" "$url"
    if ! echo "$sum  $TOOLS/$file.part" | shasum -a "$algorithm" -c --status; then
      echo "Checksum mismatch for $file. Nothing was installed." >&2
      exit 1
    fi
    mv "$TOOLS/$file.part" "$TOOLS/$file"
  fi
}

mkdir -p "$TOOLS"
fetch "$GOOGLE/build-tools_r37_macosx.zip" 1 eb080751b2b2028eb3604f571027d6f7b3c46321 build-tools.zip
fetch "$GOOGLE/platform-36_r02.zip" 1 2c1a80dd4d9f7d0e6dd336ec603d9b5c55a6f576 platform.zip
fetch "$TEMURIN/OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.13_9.tar.gz" 256 \
  bf036b35b071bfb2f54f2c0af8ee8d88afa95ee6eb3dce02c0f94cfc2524d04a jdk.tgz
[ -d "$TOOLS/bt" ] || { mkdir "$TOOLS/bt"; unzip -q "$TOOLS/build-tools.zip" -d "$TOOLS/bt"; }
[ -d "$TOOLS/pf" ] || { mkdir "$TOOLS/pf"; unzip -q "$TOOLS/platform.zip" '*/android.jar' -d "$TOOLS/pf"; }
[ -d "$TOOLS/jdk" ] || { mkdir "$TOOLS/jdk"; tar xzf "$TOOLS/jdk.tgz" -C "$TOOLS/jdk"; }

JAVA_HOME="$(echo "$TOOLS"/jdk/*/Contents/Home)"
export JAVA_HOME
BT="$(echo "$TOOLS"/bt/*)"
JAR="$(find "$TOOLS/pf" -name android.jar)"
BUILD="out/$VARIANT"

rm -rf "out/${VARIANT:?}"
mkdir -p "$BUILD/classes" "$BUILD/gen"
"$BT/aapt2" compile --dir res -o "$BUILD/res.zip"
"$BT/aapt2" link -o "$BUILD/base.apk" --manifest AndroidManifest.xml -I "$JAR" \
  --java "$BUILD/gen" ${DEBUG_FLAG[@]+"${DEBUG_FLAG[@]}"} "$BUILD/res.zip"
# shellcheck disable=SC2046
"$JAVA_HOME/bin/javac" -Xlint:all,-options -Werror -source 11 -target 11 -classpath "$JAR" -d "$BUILD/classes" \
  $(find src/main "src/$VARIANT" "$BUILD/gen" -name '*.java')
# shellcheck disable=SC2046
"$BT/d8" --release --min-api 33 --lib "$JAR" --output "$BUILD" $(find "$BUILD/classes" -name '*.class')
cp "$BUILD/base.apk" "$BUILD/unaligned.apk"
(cd "$BUILD" && zip -q -j unaligned.apk classes.dex)
"$BT/zipalign" -f -P 16 4 "$BUILD/unaligned.apk" "out/$OUT_NAME"

KEYSTORE="${WALKIE_KEYSTORE:-$TOOLS/walkie-talkie.jks}"
PASSWORD_FILE="$KEYSTORE.password"
if [ ! -f "$KEYSTORE" ]; then
  # A random password in a private file beside the key. Neither goes near the repository.
  (umask 077; openssl rand -hex 32 > "$PASSWORD_FILE")
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KEYSTORE" -storepass:file "$PASSWORD_FILE" \
    -keypass:file "$PASSWORD_FILE" -alias walkie -keyalg RSA -keysize 2048 -validity 10000 -dname CN=walkie-talkie
  chmod 600 "$KEYSTORE"
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass "file:$PASSWORD_FILE" --ks-key-alias walkie "out/$OUT_NAME"
"$BT/apksigner" verify --min-sdk-version 33 "out/$OUT_NAME"
echo "Built android/out/$OUT_NAME"
