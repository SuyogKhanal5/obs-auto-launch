#!/bin/sh
# Creates the self-signed code-signing certificate the macOS app is signed with. Run ONCE, by the
# maintainer, and keep its output private:
#
#   ci/create_macos_signing_identity.sh ~/.obsautorecorder-signing
#
# Why: macOS ties each permission grant (Accessibility, Screen Recording, System Audio Recording)
# to the app's code-signing requirement. Ad hoc signing gives no stable identity -- either every
# update loses the grants (cdhash requirement), or any program claiming the bundle id inherits
# them (identifier-only requirement; confirmed live: an impostor got Accessibility, i.e.
# keylogging, with no prompt). A certificate gives a requirement only builds signed with this
# private key satisfy, so grants survive updates and can't be borrowed.
#
# Writes into the given folder (created 700): signing.p12 and signing.p12.password. Add them to
# the GitHub repo's Actions secrets as MACOS_SIGNING_P12_BASE64 (base64 of signing.p12) and
# MACOS_SIGNING_P12_PASSWORD. Never commit them; never print them.
set -eu
OUT="${1:?usage: create_macos_signing_identity.sh <private output folder>}"
NAME="OBS Auto Recorder Code Signing"
umask 077
mkdir -p "$OUT"; chmod 700 "$OUT"
if [ -e "$OUT/signing.p12" ]; then
    echo "$OUT/signing.p12 already exists -- refusing to replace the signing identity." >&2
    echo "(Replacing it means every user re-grants permissions once; delete it by hand if that's intended.)" >&2
    exit 1
fi
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF
openssl req -x509 -newkey rsa:3072 -sha256 -days 7300 -nodes -config "$WORK/cert.cnf" \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
openssl rand -base64 32 | tr -d '\n' > "$OUT/signing.p12.password"
# -legacy: macOS's `security import` can't read OpenSSL 3's default PKCS#12 encryption.
LEGACY=""; openssl pkcs12 -help 2>&1 | grep -q -- "-legacy" && LEGACY="-legacy"
openssl pkcs12 -export $LEGACY -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$NAME" \
    -passout "file:$OUT/signing.p12.password" -out "$OUT/signing.p12"
echo "Created $OUT/signing.p12 (certificate SHA-1 $(openssl x509 -in "$WORK/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :))."
echo "Next: add it to GitHub Actions secrets without displaying it, e.g."
echo "  base64 -i \"$OUT/signing.p12\" | pbcopy        # paste as MACOS_SIGNING_P12_BASE64"
echo "  pbcopy < \"$OUT/signing.p12.password\"        # paste as MACOS_SIGNING_P12_PASSWORD"
