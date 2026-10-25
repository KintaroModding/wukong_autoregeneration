"""Lists, extracts, packs and checks Black Myth: Wukong's .pak files.

Wukong's paks are pak version 11 (UE 5.0) with two changes (CUE4Parse GAME_BlackMythWukong): the footer has two extra
bytes after the version, and a file's data header has one extra byte. The game's index and full directory index are
encrypted with AES-256 (ECB) and otherwise normal; the key is public community knowledge (the playbook keeps it in
_tools\\gamedata\\wukong\\game.json). Packing and checking a mod pak need no key and no extra Python package; reading
the game's paks needs the key and pycryptodome, and its compressed files need Oodle (oo2core_9_win64.dll); Zlib
(used by some mod tools) needs nothing extra.

Usage:
    python b1pak.py list <key hex> <out.txt> <pak> [<pak> ...]
        one line per file: pak, offset, size, uncompressed size, compression, encrypted, path
    python b1pak.py extract <key hex> <oo2core dll> <pak> <path in pak> <out file>
    python b1pak.py pack <out.pak> <mount point> <name>=<source file> [...]
        names are paths below the mount point ("Runtime/X.data"); no encryption, no compression
    python b1pak.py check <pak> <name>=<source file> [...]
        reads a pak made by pack and compares each file with its source (exit code 1 on a difference)
"""

import ctypes
import hashlib
import struct
import sys
import zlib
from pathlib import Path

MAGIC = 0x5A6F12E1
# guid 16, encrypted index 1, magic 4, version 4, Wukong extra 2, index offset 8, index size 8, hash 20, 5 names x 32
FOOTER_SIZE = 16 + 1 + 4 + 4 + 2 + 8 + 8 + 20 + 5 * 32


class Cipher:
    """AES-256-ECB, made only when something is encrypted (so a mod pak needs no pycryptodome)."""

    def __init__(self, key):
        self.key, self.aes = key, None

    def decrypt(self, data: bytes) -> bytes:
        if self.aes is None:
            if not self.key:
                raise SystemExit("this pak is encrypted: give the AES key")
            from Crypto.Cipher import AES
            self.aes = AES.new(self.key, AES.MODE_ECB)
        return self.aes.decrypt(data)


class Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def take(self, fmt: str):
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return values if len(values) > 1 else values[0]

    def fstring(self) -> str:
        length = self.take("i")
        if length == 0:
            return ""
        if length < 0:
            raw = self.data[self.pos:self.pos - length * 2]
            self.pos -= length * 2
            return raw.decode("utf-16-le").rstrip("\0")
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode("utf-8", "replace").rstrip("\0")


def read_footer(f):
    f.seek(-FOOTER_SIZE, 2)
    r = Reader(f.read(FOOTER_SIZE))
    r.pos = 16
    encrypted = r.take("B")
    magic, version, _extra = r.take("IIH")
    if magic != MAGIC:
        raise ValueError(f"no pak magic at the Wukong footer position (got {magic:#x})")
    offset, size = r.take("qq")
    r.pos += 20
    names = []
    for _ in range(5):
        name = r.data[r.pos:r.pos + 32].split(b"\0")[0].decode("ascii", "replace")
        r.pos += 32
        if name:
            names.append(name)
    return encrypted, version, offset, size, names


def read_block(f, offset, size, cipher, encrypted):
    f.seek(offset)
    data = f.read((size + 15) // 16 * 16 if encrypted else size)
    return cipher.decrypt(data)[:size] if encrypted else data


def decode_entry(r: Reader):
    value = r.take("I")
    block_size = r.take("I") if (value & 0x3F) == 0x3F else (value & 0x3F) << 11
    method = (value >> 23) & 0x3F
    offset = r.take("I") if value & (1 << 31) else r.take("q")
    uncompressed = r.take("I") if value & (1 << 30) else r.take("q")
    size = (r.take("I") if value & (1 << 29) else r.take("q")) if method else uncompressed
    encrypted = bool(value & (1 << 22))
    blocks = (value >> 6) & 0xFFFF
    lengths = [size] if blocks == 1 and not encrypted else []
    if blocks > 0 and (encrypted or blocks != 1):
        lengths = [r.take("I") for _ in range(blocks)]
    return offset, size, uncompressed, method, encrypted, block_size, lengths


def list_pak(path: Path, key):
    cipher = Cipher(key)
    with path.open("rb") as f:
        encrypted, version, index_offset, index_size, methods = read_footer(f)
        r = Reader(read_block(f, index_offset, index_size, cipher, encrypted))
        mount = r.fstring()
        r.take("i")  # number of entries
        r.take("Q")  # path hash seed
        if r.take("i"):
            r.pos += 8 + 8 + 20  # path hash index: offset, size, hash
        if not r.take("i"):
            raise ValueError("no full directory index")
        fdi_offset, fdi_size = r.take("qq")
        r.pos += 20
        encoded_size = r.take("i")
        encoded = r.data[r.pos:r.pos + encoded_size]
        fdi = Reader(read_block(f, fdi_offset, fdi_size, cipher, encrypted))
    rows = []
    for _ in range(fdi.take("i")):
        directory = fdi.fstring()
        for _ in range(fdi.take("i")):
            name = fdi.fstring()
            location = fdi.take("i")
            if location < 0:
                rows.append((None, None, None, None, None, mount + directory + name, None))
                continue
            entry = decode_entry(Reader(encoded[location:]))
            offset, size, uncompressed, method, enc = entry[:5]
            label = methods[method - 1] if 0 < method <= len(methods) else ("none" if method == 0 else str(method))
            rows.append((offset, size, uncompressed, label, enc, mount + directory + name, entry))
    return version, mount, rows


def read_file(pak: Path, row, key, dll) -> bytes:
    offset, size, uncompressed, method, encrypted, block_size, lengths = row[6]
    # The data header: Offset (8), Wukong's extra byte, Size, UncompressedSize (8 each), method (4), hash (20), the
    # block table when compressed (4 + 16 per block), flags (1), block size (4).
    header = 8 * 3 + 4 * 2 + 1 + 20 + (4 + len(lengths) * 16 if method else 0) + 1
    cipher = Cipher(key)
    with pak.open("rb") as f:
        position = offset + header
        if not method:
            return read_block(f, position, size, cipher, encrypted)
        if row[3] == "Zlib":
            # Mod paks made with other tools use Zlib (the game's own paks use Oodle).
            parts = []
            for length in lengths:
                parts.append(zlib.decompress(read_block(f, position, length, cipher, encrypted)))
                position += (length + 15) // 16 * 16 if encrypted else length
            return b"".join(parts)
        oodle = ctypes.WinDLL(str(dll))
        decompress = oodle.OodleLZ_Decompress
        decompress.restype = ctypes.c_ssize_t
        decompress.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t] + [ctypes.c_int] * 3 + \
            [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int]
        parts, left = [], uncompressed
        for length in lengths:
            block = read_block(f, position, length, cipher, encrypted)
            position += (length + 15) // 16 * 16 if encrypted else length
            raw_size = min(block_size or uncompressed, left)
            raw = ctypes.create_string_buffer(raw_size)
            got = decompress(block, len(block), raw, raw_size, 1, 0, 0, None, 0, None, None, None, 0, 3)
            if got != raw_size:
                raise SystemExit(f"Oodle returned {got} instead of {raw_size} bytes")
            parts.append(raw.raw)
            left -= raw_size
        return b"".join(parts)


def extract(key: bytes, dll: Path, pak: Path, wanted: str, out: Path):
    _, _, rows = list_pak(pak, key)
    row = next((r for r in rows if r[5] == wanted), None)
    if row is None or row[6] is None:
        raise SystemExit(f"{wanted} is not in {pak.name}")
    data = read_file(pak, row, key, dll)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    _, size, _, _, encrypted, _, lengths = row[6]
    print(f"{wanted}: {len(data)} bytes (packed {size}, {'encrypted, ' if encrypted else ''}{len(lengths)} block(s)) -> {out}")


def fstring(text: str) -> bytes:
    raw = text.encode("ascii") + b"\0"
    return struct.pack("<i", len(raw)) + raw


def path_hash(relative: str, seed: int) -> int:
    """UE5 FPakFile::HashPath: FNV-64 of the lowercase UTF-16 path below the mount point, started at offset + seed."""
    value = (0xCBF29CE484222325 + seed) & 0xFFFFFFFFFFFFFFFF
    for byte in relative.lower().encode("utf-16-le"):
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def pack(out: Path, mount: str, files: list):
    """Writes a pak laid out like the game's own: one file at the root ("/") like pakchunk23, several folders like
    pakchunk9 (the parent folders in the full directory index with 0 files, an empty pruned index after the path
    hashes)."""
    body, encoded, locations = bytearray(), bytearray(), []
    for name, source in files:
        data = source.read_bytes()
        offset = len(body)
        # Data header: Offset 0, Wukong's extra byte, Size, UncompressedSize, method 0, SHA-1, flags 0, block size 0.
        body += struct.pack("<qBqqI", 0, 0, len(data), len(data), 0) + hashlib.sha1(data).digest() + struct.pack("<BI", 0, 0)
        body += data
        locations.append((name, len(encoded)))
        # Encoded entry: offset, uncompressed size and size fit 32 bits; no compression, no blocks, not encrypted.
        encoded += struct.pack("<III", 0xE0000000, offset, len(data))
    # Folders below the mount point ("/" for the root, else "Runtime/"), each with its parents.
    folders = {"/": []}
    for name, location in locations:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            folders.setdefault("/".join(parts[:depth]) + "/", [])
        folders["/".join(parts[:-1]) + "/" if len(parts) > 1 else "/"].append((parts[-1], location))
    fdi = struct.pack("<i", len(folders))
    for folder, entries in folders.items():
        fdi += fstring(folder) + struct.pack("<i", len(entries))
        for leaf, location in entries:
            fdi += fstring(leaf) + struct.pack("<i", location)
    seed = 0
    phi = struct.pack("<i", len(locations))
    for name, location in locations:
        phi += struct.pack("<Qi", path_hash(name, seed), location)
    if len(folders) == 1:
        phi += fdi + struct.pack("<i", len(locations))  # pakchunk23: the full directory index again, then the file count
    else:
        phi += struct.pack("<i", 0)  # pakchunk9: an empty pruned directory index
    index_offset = len(body)
    index_size = len(fstring(mount)) + 4 + 8 + 4 + 36 + 4 + 36 + 4 + len(encoded) + 4
    phi_offset = index_offset + index_size
    fdi_offset = phi_offset + len(phi)
    index = fstring(mount) + struct.pack("<iQ", len(locations), seed)
    index += struct.pack("<iqq", 1, phi_offset, len(phi)) + hashlib.sha1(phi).digest()
    index += struct.pack("<iqq", 1, fdi_offset, len(fdi)) + hashlib.sha1(fdi).digest()
    index += struct.pack("<i", len(encoded)) + bytes(encoded) + struct.pack("<i", 0)
    assert len(index) == index_size
    footer = bytes(16) + struct.pack("<BIIH", 0, MAGIC, 11, 0x10) + struct.pack("<qq", index_offset, len(index))
    footer += hashlib.sha1(index).digest() + b"Oodle".ljust(32, b"\0") + bytes(4 * 32)
    assert len(footer) == FOOTER_SIZE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(body) + index + phi + fdi + footer)
    print(f"{out}: {len(files)} file(s), {out.stat().st_size} bytes, mount {mount}")


def check(pak: Path, files: list) -> bool:
    _, mount, rows = list_pak(pak, None)
    by_path = {r[5]: r for r in rows}
    ok = len(rows) == len(files)
    if not ok:
        print(f"{pak.name}: {len(rows)} files in the pak, {len(files)} expected")
    for name, source in files:
        folder, _, leaf = name.rpartition("/")
        row = by_path.get(mount + (folder + "/" if folder else "/") + leaf)
        same = row is not None and read_file(pak, row, None, None) == source.read_bytes()
        print(f"{'OK' if same else 'DIFFERS'}: {name}")
        ok = ok and same
    return ok


def file_args(items):
    return [(item.split("=", 1)[0], Path(item.split("=", 1)[1])) for item in items]


def main():
    command = sys.argv[1]
    if command == "pack":
        pack(Path(sys.argv[2]), sys.argv[3], file_args(sys.argv[4:]))
        return
    if command == "check":
        sys.exit(0 if check(Path(sys.argv[2]), file_args(sys.argv[3:])) else 1)
    if command == "extract":
        key = bytes.fromhex(sys.argv[2].removeprefix("0x"))
        extract(key, Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], Path(sys.argv[6]))
        return
    key = bytes.fromhex(sys.argv[2].removeprefix("0x"))
    out = Path(sys.argv[3])
    lines = []
    for name in sys.argv[4:]:
        path = Path(name)
        try:
            version, mount, rows = list_pak(path, key)
        except Exception as error:  # Report and go on with the next pak.
            print(f"{path.name}: ERROR {error}")
            continue
        print(f"{path.name}: version {version}, mount {mount}, {len(rows)} files")
        for offset, size, uncompressed, method, enc, file, _ in rows:
            lines.append(f"{path.stem}\t{offset}\t{size}\t{uncompressed}\t{method}\t{int(bool(enc))}\t{file}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} files -> {out}")


if __name__ == "__main__":
    main()
