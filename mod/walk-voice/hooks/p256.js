// Checks an ECDSA P-256 signature with SHA-256, the kind the phone's secure hardware makes after a
// fingerprint. The mod's environment has no signature check, so this is the check, in BigInt, with
// its own SHA-256 so that it answers in one step. It only ever checks public data: the key, the
// message, and the signature.
//
// The key is base64 of its X.509 form, and the signature base64 of its DER form, as Android gives
// them. Anything malformed is simply not a valid signature.

const P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffffn
const N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551n
const B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604bn
const G = [
  0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296n,
  0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5n,
  1n,
]
// The fixed start of a P-256 public key's X.509 form, before the point's 65 bytes.
const KEY_PREFIX = '3059301306072a8648ce3d020106082a8648ce3d030107034200'

// SHA-256, from FIPS 180-4.
const K = Uint32Array.from(
  '428a2f98 71374491 b5c0fbcf e9b5dba5 3956c25b 59f111f1 923f82a4 ab1c5ed5 d807aa98 12835b01 243185be 550c7dc3 72be5d74 80deb1fe 9bdc06a7 c19bf174 e49b69c1 efbe4786 0fc19dc6 240ca1cc 2de92c6f 4a7484aa 5cb0a9dc 76f988da 983e5152 a831c66d b00327c8 bf597fc7 c6e00bf3 d5a79147 06ca6351 14292967 27b70a85 2e1b2138 4d2c6dfc 53380d13 650a7354 766a0abb 81c2c92e 92722c85 a2bfe8a1 a81a664b c24b8b70 c76c51a3 d192e819 d6990624 f40e3585 106aa070 19a4c116 1e376c08 2748774c 34b0bcb5 391c0cb3 4ed8aa4a 5b9cca4f 682e6ff3 748f82ee 78a5636f 84c87814 8cc70208 90befffa a4506ceb bef9a3f7 c67178f2'
    .split(' ')
    .map((word) => parseInt(word, 16)),
)

export function sha256(bytes) {
  const length = bytes.length
  const padded = new Uint8Array(Math.ceil((length + 9) / 64) * 64)
  padded.set(bytes)
  padded[length] = 0x80
  const bits = new DataView(padded.buffer)
  bits.setUint32(padded.length - 8, Math.floor(length / 0x20000000))
  bits.setUint32(padded.length - 4, (length * 8) >>> 0)
  const h = Uint32Array.of(0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19)
  const w = new Uint32Array(64)
  const rotate = (x, n) => (x >>> n) | (x << (32 - n))
  for (let block = 0; block < padded.length; block += 64) {
    for (let i = 0; i < 16; i++) w[i] = bits.getUint32(block + i * 4)
    for (let i = 16; i < 64; i++) {
      const s0 = rotate(w[i - 15], 7) ^ rotate(w[i - 15], 18) ^ (w[i - 15] >>> 3)
      const s1 = rotate(w[i - 2], 17) ^ rotate(w[i - 2], 19) ^ (w[i - 2] >>> 10)
      w[i] = w[i - 16] + s0 + w[i - 7] + s1
    }
    let [a, b, c, d, e, f, g, k] = h
    for (let i = 0; i < 64; i++) {
      const t1 = k + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]
      const t2 = (rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))
      k = g
      g = f
      f = e
      e = (d + t1) >>> 0
      d = c
      c = b
      b = a
      a = (t1 + t2) >>> 0
    }
    h[0] += a
    h[1] += b
    h[2] += c
    h[3] += d
    h[4] += e
    h[5] += f
    h[6] += g
    h[7] += k
  }
  const out = new Uint8Array(32)
  const view = new DataView(out.buffer)
  h.forEach((word, i) => view.setUint32(i * 4, word))
  return out
}

const mod = (a, m = P) => ((a % m) + m) % m

function power(base, exponent, m) {
  let result = 1n
  base = mod(base, m)
  while (exponent > 0n) {
    if (exponent & 1n) result = (result * base) % m
    base = (base * base) % m
    exponent >>= 1n
  }
  return result
}

const inverse = (a, m) => power(a, m - 2n, m) // both moduli are prime

// Points in Jacobian coordinates [X, Y, Z]; null is the point at infinity.
function double(point) {
  if (point === null) return null
  const [x, y, z] = point
  if (y === 0n) return null
  const delta = mod(z * z)
  const gamma = mod(y * y)
  const beta = mod(x * gamma)
  const alpha = mod(3n * (x - delta) * (x + delta))
  const x3 = mod(alpha * alpha - 8n * beta)
  const z3 = mod((y + z) * (y + z) - gamma - delta)
  const y3 = mod(alpha * (4n * beta - x3) - 8n * gamma * gamma)
  return [x3, y3, z3]
}

function add(p, q) {
  if (p === null) return q
  if (q === null) return p
  const [x1, y1, z1] = p
  const [x2, y2, z2] = q
  const z1z1 = mod(z1 * z1)
  const z2z2 = mod(z2 * z2)
  const u1 = mod(x1 * z2z2)
  const u2 = mod(x2 * z1z1)
  const s1 = mod(y1 * z2 * z2z2)
  const s2 = mod(y2 * z1 * z1z1)
  if (u1 === u2) return s1 === s2 ? double(p) : null
  const h = mod(u2 - u1)
  const r = mod(s2 - s1)
  const hh = mod(h * h)
  const hhh = mod(h * hh)
  const x3 = mod(r * r - hhh - 2n * u1 * hh)
  const y3 = mod(r * (u1 * hh - x3) - s1 * hhh)
  return [x3, y3, mod(h * z1 * z2)]
}

function multiply(k, point) {
  let result = null
  for (let bit = BigInt(k.toString(2).length) - 1n; bit >= 0n; bit--) {
    result = double(result)
    if ((k >> bit) & 1n) result = add(result, point)
  }
  return result
}

function bytesOf(base64) {
  if (typeof base64 !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/.test(base64)) return null
  try {
    return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0))
  } catch {
    return null
  }
}

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
const number = (bytes) => (bytes.length ? BigInt(`0x${hex(bytes)}`) : 0n)

function publicPoint(key) {
  const bytes = bytesOf(key)
  if (!bytes || bytes.length !== 91 || hex(bytes.subarray(0, 26)) !== KEY_PREFIX || bytes[26] !== 4) return null
  const x = number(bytes.subarray(27, 59))
  const y = number(bytes.subarray(59, 91))
  if (x >= P || y >= P || mod(y * y) !== mod(x * x * x - 3n * x + B)) return null // on the curve
  return [x, y, 1n]
}

// SEQUENCE { INTEGER r, INTEGER s }, each with a short length.
function signatureParts(signature) {
  const der = bytesOf(signature)
  if (!der || der.length < 8 || der[0] !== 0x30 || der[1] !== der.length - 2) return null
  const parts = []
  let at = 2
  for (let i = 0; i < 2; i++) {
    if (der[at] !== 0x02) return null
    const length = der[at + 1]
    if (length < 1 || length > 33 || at + 2 + length > der.length) return null
    const value = der.subarray(at + 2, at + 2 + length)
    if (value[0] & 0x80) return null // negative
    parts.push(number(value))
    at += 2 + length
  }
  return at === der.length ? parts : null
}

// Whether signature signs message under key.
export function verify(key, message, signature) {
  const q = publicPoint(key)
  const parts = signatureParts(signature)
  if (!q || !parts || typeof message !== 'string') return false
  const [r, s] = parts
  if (r <= 0n || r >= N || s <= 0n || s >= N) return false
  const e = number(sha256(new TextEncoder().encode(message)))
  const w = inverse(s, N)
  const point = add(multiply(mod(e * w, N), G), multiply(mod(r * w, N), q))
  if (point === null) return false
  const [x, , z] = point
  const affineX = mod(x * inverse(mod(z * z), P))
  return mod(affineX, N) === r
}

// The curve's arithmetic, for tests that sign as the phone does.
export const curve = { P, N, G, KEY_PREFIX, mod, inverse, multiply, add }
