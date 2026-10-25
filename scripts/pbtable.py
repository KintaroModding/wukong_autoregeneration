"""Decodes Wukong's protobuf tables (PBTable/*.data) to JSON with field names and encodes them back.

The schema comes from pbschema.py. A table file <Name>[-<part>].data holds the message TB<Name> (field 1: the rows).
Messages become JSON objects in field-number order (the order Google.Protobuf writes them), enums their names, maps
lists of {"key", "value"}; fields that the schema does not know stay as "#<number>" with their raw wire value.

Usage:
    python pbtable.py decode <schema.json> <in.data> <out.json> [<root type>]
    python pbtable.py encode <schema.json> <in.json> <out.data> [<root type>]
    python pbtable.py check  <schema.json> <in.data> [<root type>]     decode, encode, compare the bytes
    python pbtable.py verify <schema.json> <in.json> <in.data>         decode the table, compare it with the JSON
    python pbtable.py subset <schema.json> <out.json> <root type> [...]
        the part of the schema that the root types use (merged into <out.json> when it exists)
check and verify end with exit code 1 when the result differs or the table holds fields the schema does not know.
"""

import json
import struct
import sys
from pathlib import Path

PACKABLE = {"int32", "int64", "uint32", "uint64", "sint32", "sint64", "bool", "enum", "float", "double", "fixed32",
            "fixed64", "sfixed32", "sfixed64"}


class Schema:
    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.messages, self.enums = data["messages"], data["enums"]
        self.by_short = {}
        for full in list(self.messages) + list(self.enums):
            self.by_short.setdefault(full.rsplit(".", 1)[-1], []).append(full)

    def resolve(self, short: str, context: str) -> str:
        ns = context.rsplit(".", 1)[0]
        if f"{ns}.{short}" in self.messages or f"{ns}.{short}" in self.enums:
            return f"{ns}.{short}"
        found = self.by_short.get(short, [])
        if len(found) == 1:
            return found[0]
        raise KeyError(f"type {short} (from {context}) is {'unknown' if not found else 'ambiguous: ' + ', '.join(found)}")

    def root_for(self, file: Path) -> str:
        found = self.by_short.get("TB" + file.stem.split("-")[0], [])
        if len(found) != 1:
            raise KeyError(f"no single root type for {file.name}: {found}; give it as the last argument")
        return found[0]


# Wire format


def read_varint(data, pos):
    result = shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def write_varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def scalar_from_varint(kind: str, raw: int):
    if kind in ("int32", "enum"):
        return signed(raw, 32)
    if kind == "int64":
        return signed(raw, 64)
    if kind == "sint32" or kind == "sint64":
        return (raw >> 1) ^ -(raw & 1)
    if kind == "bool":
        return bool(raw)
    return raw


def scalar_to_varint(kind: str, value) -> int:
    if kind in ("sint32", "sint64"):
        return (value << 1) ^ (value >> 63)
    return int(value)


def decode_message(data: bytes, type_name: str, schema: Schema) -> dict:
    fields = schema.messages[type_name]
    out, pos = {}, 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            raw, pos = read_varint(data, pos)
        elif wire == 1:
            raw, pos = data[pos:pos + 8], pos + 8
        elif wire == 5:
            raw, pos = data[pos:pos + 4], pos + 4
        elif wire == 2:
            length, pos = read_varint(data, pos)
            raw, pos = data[pos:pos + length], pos + length
        else:
            raise ValueError(f"wire type {wire} in {type_name}")
        spec = fields.get(str(number))
        if spec is None:
            out.setdefault(f"#{number}", []).append({"wire": wire, "raw": raw.hex() if isinstance(raw, bytes) else raw})
            continue
        name = spec["name"]
        if spec["label"] == "map":
            out.setdefault(name, []).append(decode_map_entry(raw, spec, schema, type_name))
        elif spec["label"] == "repeated":
            kind, ref = spec["type"]
            if kind in PACKABLE and wire == 2:
                out.setdefault(name, []).extend(unpack(raw, kind, ref, schema, type_name))
            else:
                out.setdefault(name, []).append(decode_value(raw, wire, kind, ref, schema, type_name))
        else:
            kind, ref = spec["type"]
            out[name] = decode_value(raw, wire, kind, ref, schema, type_name)
    return out


def decode_value(raw, wire, kind, ref, schema, context):
    if kind == "message":
        return decode_message(raw, schema.resolve(ref, context), schema)
    if kind == "string":
        return raw.decode("utf-8")
    if kind == "bytes":
        return {"bytes": raw.hex()}
    if kind == "enum":
        value = scalar_from_varint("enum", raw)
        names = schema.enums.get(schema.resolve(ref, context), {}) if ref else {}
        return names.get(str(value), value)
    if wire == 5:
        return struct.unpack("<f" if kind == "float" else ("<i" if kind == "sfixed32" else "<I"), raw)[0]
    if wire == 1:
        return struct.unpack("<d" if kind == "double" else ("<q" if kind == "sfixed64" else "<Q"), raw)[0]
    return scalar_from_varint(kind, raw)


def unpack(raw, kind, ref, schema, context):
    values, pos = [], 0
    while pos < len(raw):
        if kind in ("float", "fixed32", "sfixed32"):
            values.append(decode_value(raw[pos:pos + 4], 5, kind, ref, schema, context))
            pos += 4
        elif kind in ("double", "fixed64", "sfixed64"):
            values.append(decode_value(raw[pos:pos + 8], 1, kind, ref, schema, context))
            pos += 8
        else:
            v, pos = read_varint(raw, pos)
            values.append(decode_value(v, 0, kind, ref, schema, context))
    return values


def decode_map_entry(raw, spec, schema, context):
    entry, pos = {}, 0
    while pos < len(raw):
        tag, pos = read_varint(raw, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = read_varint(raw, pos)
        elif wire == 2:
            length, pos = read_varint(raw, pos)
            value, pos = raw[pos:pos + length], pos + length
        elif wire == 5:
            value, pos = raw[pos:pos + 4], pos + 4
        else:
            value, pos = raw[pos:pos + 8], pos + 8
        kind, ref = spec["key"] if number == 1 else spec["value"]
        entry["key" if number == 1 else "value"] = decode_value(value, wire, kind, ref, schema, context)
    return entry


def encode_message(obj: dict, type_name: str, schema: Schema) -> bytes:
    fields = schema.messages[type_name]
    by_name = {spec["name"]: (int(number), spec) for number, spec in fields.items()}
    out = bytearray()
    unknown = [(int(k[1:]), k) for k in obj if k.startswith("#")]
    known = [(by_name[k][0], k) for k in obj if not k.startswith("#")]
    for number, key in sorted(known + unknown):
        if key.startswith("#"):
            for item in obj[key]:
                out += write_varint(number << 3 | item["wire"])
                raw = item["raw"]
                if item["wire"] == 0:
                    out += write_varint(raw)
                else:
                    raw = bytes.fromhex(raw)
                    out += (write_varint(len(raw)) if item["wire"] == 2 else b"") + raw
            continue
        _, spec = by_name[key]
        value = obj[key]
        if spec["label"] == "map":
            for entry in value:
                body = encode_value(1, entry["key"], *spec["key"], schema, type_name) + \
                    encode_value(2, entry["value"], *spec["value"], schema, type_name)
                out += write_varint(number << 3 | 2) + write_varint(len(body)) + body
        elif spec["label"] == "repeated":
            kind, ref = spec["type"]
            if kind in PACKABLE:
                body = b"".join(encode_scalar(v, kind, ref, schema, type_name)[1] for v in value)
                out += write_varint(number << 3 | 2) + write_varint(len(body)) + body
            else:
                for v in value:
                    out += encode_value(number, v, kind, ref, schema, type_name)
        else:
            out += encode_value(number, value, *spec["type"], schema, type_name)
    return bytes(out)


def encode_scalar(value, kind, ref, schema, context):
    if kind == "enum" and isinstance(value, str):
        names = schema.enums[schema.resolve(ref, context)]
        value = int(next(k for k, v in names.items() if v == value))
    if kind == "float":
        return 5, struct.pack("<f", value)
    if kind == "double":
        return 1, struct.pack("<d", value)
    if kind in ("fixed32", "sfixed32"):
        return 5, struct.pack("<i" if kind == "sfixed32" else "<I", value)
    if kind in ("fixed64", "sfixed64"):
        return 1, struct.pack("<q" if kind == "sfixed64" else "<Q", value)
    return 0, write_varint(scalar_to_varint(kind, value))


def encode_value(number, value, kind, ref, schema, context):
    if kind == "message":
        body = encode_message(value, schema.resolve(ref, context), schema)
    elif kind == "string":
        body = value.encode("utf-8")
    elif kind == "bytes":
        body = bytes.fromhex(value["bytes"])
    else:
        wire, body = encode_scalar(value, kind, ref, schema, context)
        return write_varint(number << 3 | wire) + body
    return write_varint(number << 3 | 2) + write_varint(len(body)) + body


def main():
    command, schema = sys.argv[1], Schema(Path(sys.argv[2]))
    if command == "decode":
        src, out = Path(sys.argv[3]), Path(sys.argv[4])
        root = sys.argv[5] if len(sys.argv) > 5 else schema.root_for(src)
        obj = decode_message(src.read_bytes(), root, schema)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"$type": root, **obj}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{src.name} ({root}) -> {out}")
    elif command == "encode":
        src, out = Path(sys.argv[3]), Path(sys.argv[4])
        obj = json.loads(src.read_text(encoding="utf-8"))
        root = sys.argv[5] if len(sys.argv) > 5 else obj.pop("$type")
        obj.pop("$type", None)
        out.write_bytes(encode_message(obj, root, schema))
        print(f"{src.name} ({root}) -> {out}")
    elif command == "check":
        src = Path(sys.argv[3])
        root = sys.argv[4] if len(sys.argv) > 4 else schema.root_for(src)
        data = src.read_bytes()
        obj = decode_message(data, root, schema)
        again = encode_message(obj, root, schema)
        rows = next(iter(obj.values()), [])
        unknown = json.dumps(obj).count('"#')
        print(f"{src.name}: {root}, {len(rows) if isinstance(rows, list) else 1} rows, unknown fields {unknown}, "
              f"round trip {'identical' if again == data else f'DIFFERS ({len(again)} vs {len(data)} bytes)'}")
        sys.exit(0 if again == data and not unknown else 1)
    elif command == "verify":
        src, built = Path(sys.argv[3]), Path(sys.argv[4])
        obj = json.loads(src.read_text(encoding="utf-8"))
        root = obj.pop("$type")
        data = built.read_bytes()
        again = decode_message(data, root, schema)
        unknown = json.dumps(again).count('"#')

        def f32(value):
            """Floats at float32 precision (an edited 0.1 is stored as 0.10000000149011612)."""
            if isinstance(value, float):
                return struct.unpack("<f", struct.pack("<f", value))[0]
            if isinstance(value, dict):
                return {k: f32(v) for k, v in value.items()}
            if isinstance(value, list):
                return [f32(v) for v in value]
            return value

        same = f32(again) == f32(obj) and encode_message(again, root, schema) == data
        print(f"{'OK' if same and not unknown else 'DIFFERS'}: {built.name} ({root}) against {src.name}")
        sys.exit(0 if same and not unknown else 1)
    elif command == "subset":
        write_subset(schema, Path(sys.argv[3]), sys.argv[4:])


def write_subset(schema: Schema, out: Path, roots):
    """Writes the messages and enums that the root types use into out (merged with what it holds already)."""
    part = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"messages": {}, "enums": {}}
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in part["messages"] or name in part["enums"]:
            continue
        if name in schema.enums:
            part["enums"][name] = schema.enums[name]
            continue
        part["messages"][name] = schema.messages[name]
        for spec in schema.messages[name].values():
            for kind_ref in (spec.get("type"), spec.get("key"), spec.get("value")):
                if kind_ref and kind_ref[0] in ("message", "enum") and kind_ref[1]:
                    todo.append(schema.resolve(kind_ref[1], name))
    part = {"messages": dict(sorted(part["messages"].items())), "enums": dict(sorted(part["enums"].items()))}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(part, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{out.name}: {len(part['messages'])} messages, {len(part['enums'])} enums")


if __name__ == "__main__":
    main()
