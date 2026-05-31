"""Tiny protobuf wire encoder/decoder for JSON `.protodef` schemas.

This is intentionally *not* a protobuf compiler. It only knows enough schema
metadata to turn dictionaries into protobuf wire bytes and back again.

Supported field format, version 1::

    {
      "version": 1,
      "namespace": "Media",          # optional; falls back to file stem
      "imports": ["common.protodef"], # optional; defaults to []
      "messages": {
        "File": {
          "fields": {
            "fileId": {"t": 1, "k": "fixed64"},
            "name":   {"t": 2, "k": "string", "o": true},
            "tags":   {"t": 3, "k": "string", "r": true}
          }
        }
      }
    }

`t` = protobuf tag number, `k` = kind/type, `r` = repeated, `o` = optional
(metadata only), `oneof` = mutually exclusive group hint.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping


class ProtoDefError(ValueError):
    """Raised when a schema or protobuf payload is invalid for this runtime."""


# Protobuf wire type constants.
WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


VARINT_TYPES = {"uint32", "uint64", "int32", "int64", "sint32", "sint64", "bool", "enum"}
FIXED64_TYPES = {"fixed64", "sfixed64", "double"}
FIXED32_TYPES = {"fixed32", "sfixed32", "float"}
LEN_TYPES = {"string", "bytes"}
PRIMITIVE_TYPES = VARINT_TYPES | FIXED64_TYPES | FIXED32_TYPES | LEN_TYPES
PACKABLE_TYPES = VARINT_TYPES | FIXED64_TYPES | FIXED32_TYPES


@dataclass(frozen=True)
class FieldDef:
    name: str
    tag: int
    kind: str
    optional: bool = False
    repeated: bool = False
    packed: bool = False
    oneof: str | None = None
    owner: str = ""
    owner_namespace: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageDef:
    name: str
    full_name: str
    namespace: str
    fields: Mapping[str, FieldDef]
    by_tag: Mapping[int, FieldDef]


class Schema:
    """Resolved `.protodef` schema registry.

    Use `Schema.from_file(path)` for real projects; it recursively resolves
    imports relative to the importing file.
    """

    def __init__(self) -> None:
        self.messages: dict[str, MessageDef] = {}
        self.aliases: dict[str, str | None] = {}
        self.loaded_files: list[Path] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "Schema":
        schema = cls()
        schema._load_file(Path(path).expanduser().resolve(), stack=[])
        return schema

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, namespace: str = "") -> "Schema":
        schema = cls()
        schema._load_dict(data, base_dir=None, namespace=namespace, source_name="<dict>", stack=[])
        return schema

    def encode(self, message_name: str, value: Mapping[str, Any]) -> bytes:
        msg = self.get_message(message_name)
        if not isinstance(value, Mapping):
            raise ProtoDefError(f"{message_name} value must be a mapping/dict")
        return _encode_message(self, msg, value)

    def decode(self, message_name: str, data: bytes | bytearray | memoryview, *, include_unknown: bool = False) -> dict[str, Any]:
        msg = self.get_message(message_name)
        return _decode_message(self, msg, bytes(data), include_unknown=include_unknown)

    def get_message(self, name: str, *, current_namespace: str = "") -> MessageDef:
        resolved = self.resolve_type(name, current_namespace=current_namespace)
        if resolved in PRIMITIVE_TYPES:
            raise ProtoDefError(f"{name!r} is a primitive, not a message")
        try:
            return self.messages[resolved]
        except KeyError as exc:
            raise ProtoDefError(f"unknown message type {name!r}") from exc

    def resolve_type(self, kind: str, *, current_namespace: str = "") -> str:
        """Resolve a primitive or message name to a canonical type name."""
        if kind in PRIMITIVE_TYPES:
            return kind

        candidates: list[str] = []
        if current_namespace and "." not in kind:
            candidates.append(f"{current_namespace}.{kind}")
        candidates.append(kind)

        # Convenience: if a schema defines `namespace: Media` and fields use
        # `Media.File`, the canonical name is exactly that. If a user forgot the
        # namespace but the unqualified alias is unique, it also resolves.
        for candidate in candidates:
            if candidate in self.messages:
                return candidate
            alias = self.aliases.get(candidate)
            if alias:
                return alias
            if alias is None and candidate in self.aliases:
                raise ProtoDefError(f"ambiguous message type alias {candidate!r}; use a fully qualified name")

        raise ProtoDefError(f"unknown type {kind!r}")

    def _load_file(self, path: Path, *, stack: list[Path]) -> None:
        if path in self.loaded_files:
            return
        if path in stack:
            cycle = " -> ".join(str(p) for p in [*stack, path])
            raise ProtoDefError(f"cyclic protodef import: {cycle}")
        if not path.exists():
            raise ProtoDefError(f"protodef file not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProtoDefError(f"invalid JSON in {path}: {exc}") from exc

        namespace = str(data.get("namespace") or data.get("package") or path.stem)
        self._load_dict(data, base_dir=path.parent, namespace=namespace, source_name=str(path), stack=[*stack, path])
        self.loaded_files.append(path)

    def _load_dict(
        self,
        data: Mapping[str, Any],
        *,
        base_dir: Path | None,
        namespace: str,
        source_name: str,
        stack: list[Path],
    ) -> None:
        version = data.get("version")
        if version != 1:
            raise ProtoDefError(f"{source_name}: unsupported protodef version {version!r}; expected 1")

        imports = data.get("imports", [])
        if imports is None:
            imports = []
        if not isinstance(imports, list):
            raise ProtoDefError(f"{source_name}: imports must be a list")

        for imp in imports:
            if not isinstance(imp, str):
                raise ProtoDefError(f"{source_name}: import entries must be strings")
            if base_dir is None:
                raise ProtoDefError(f"{source_name}: cannot resolve imports without a base directory")
            self._load_file((base_dir / imp).resolve(), stack=stack)

        messages_obj = data.get("messages")
        if messages_obj is None:
            # Optional fallback for early sketches where top-level message names
            # were direct keys. Ignore known metadata keys.
            messages_obj = {
                k: v
                for k, v in data.items()
                if k not in {"version", "namespace", "package", "imports"} and isinstance(v, Mapping)
            }
        if not isinstance(messages_obj, Mapping):
            raise ProtoDefError(f"{source_name}: messages must be an object")

        for short_name, msg_obj in messages_obj.items():
            if not isinstance(short_name, str) or not short_name:
                raise ProtoDefError(f"{source_name}: message names must be non-empty strings")
            if not isinstance(msg_obj, Mapping):
                raise ProtoDefError(f"{source_name}: message {short_name!r} must be an object")

            full_name = short_name if "." in short_name or not namespace else f"{namespace}.{short_name}"
            msg_namespace = full_name.rsplit(".", 1)[0] if "." in full_name else namespace
            fields_obj = msg_obj.get("fields", {})
            if not isinstance(fields_obj, Mapping):
                raise ProtoDefError(f"{source_name}: message {short_name!r}.fields must be an object")

            fields: dict[str, FieldDef] = {}
            by_tag: dict[int, FieldDef] = {}
            oneof_to_field: dict[str, str] = {}

            for field_name, field_obj in fields_obj.items():
                field_def = _parse_field(
                    source_name=source_name,
                    message_name=full_name,
                    message_namespace=msg_namespace,
                    field_name=field_name,
                    field_obj=field_obj,
                )
                if field_def.tag in by_tag:
                    other = by_tag[field_def.tag]
                    raise ProtoDefError(
                        f"{source_name}: {full_name} field {field_name!r} reuses tag {field_def.tag} "
                        f"already used by {other.name!r}"
                    )
                if field_def.oneof:
                    # This is only metadata, but duplicate field names are useful
                    # to catch early. Multiple fields in a oneof are expected.
                    oneof_to_field.setdefault(field_def.oneof, field_name)

                fields[field_name] = field_def
                by_tag[field_def.tag] = field_def

            if full_name in self.messages:
                raise ProtoDefError(f"{source_name}: duplicate message definition {full_name!r}")
            msg = MessageDef(name=short_name.rsplit(".", 1)[-1], full_name=full_name, namespace=msg_namespace, fields=fields, by_tag=by_tag)
            self.messages[full_name] = msg
            self._add_alias(full_name, full_name)
            self._add_alias(msg.name, full_name)

    def _add_alias(self, alias: str, full_name: str) -> None:
        existing = self.aliases.get(alias)
        if existing is None and alias in self.aliases:
            return
        if existing is not None and existing != full_name:
            self.aliases[alias] = None  # mark ambiguous
        else:
            self.aliases[alias] = full_name


def _parse_field(
    *,
    source_name: str,
    message_name: str,
    message_namespace: str,
    field_name: str,
    field_obj: Any,
) -> FieldDef:
    if not isinstance(field_name, str) or not field_name:
        raise ProtoDefError(f"{source_name}: {message_name} has an invalid field name {field_name!r}")
    if not isinstance(field_obj, Mapping):
        raise ProtoDefError(f"{source_name}: {message_name}.{field_name} must be an object")

    tag = field_obj.get("t", field_obj.get("tag"))
    kind = field_obj.get("k", field_obj.get("type"))
    if not isinstance(tag, int) or tag <= 0 or tag >= (1 << 29):
        raise ProtoDefError(f"{source_name}: {message_name}.{field_name} has invalid tag {tag!r}")
    if not isinstance(kind, str) or not kind:
        raise ProtoDefError(f"{source_name}: {message_name}.{field_name} has invalid kind/type {kind!r}")
    if 19000 <= tag <= 19999:
        raise ProtoDefError(f"{source_name}: {message_name}.{field_name} uses reserved protobuf tag {tag}")

    repeated = bool(field_obj.get("r", field_obj.get("repeated", False)))
    packed = bool(field_obj.get("packed", False))
    if packed and not repeated:
        raise ProtoDefError(f"{source_name}: {message_name}.{field_name} has packed=true but is not repeated")

    return FieldDef(
        name=field_name,
        tag=tag,
        kind=kind,
        optional=bool(field_obj.get("o", field_obj.get("optional", False))),
        repeated=repeated,
        packed=packed,
        oneof=field_obj.get("oneof"),
        owner=message_name,
        owner_namespace=message_namespace,
        raw=dict(field_obj),
    )


# Public convenience API.

def load_schema(path: str | Path) -> Schema:
    return Schema.from_file(path)


def encode_message(schema: Schema, message_name: str, value: Mapping[str, Any]) -> bytes:
    return schema.encode(message_name, value)


def decode_message(schema: Schema, message_name: str, data: bytes | bytearray | memoryview, *, include_unknown: bool = False) -> dict[str, Any]:
    return schema.decode(message_name, data, include_unknown=include_unknown)


# Encoding implementation.

def _encode_message(schema: Schema, msg: MessageDef, value: Mapping[str, Any]) -> bytes:
    out = bytearray()

    # Treat oneof as a safety check for sender UX. It does not affect bytes.
    seen_oneofs: dict[str, str] = {}
    for field_name, field_value in value.items():
        field_def = msg.fields.get(field_name)
        if field_def is None:
            raise ProtoDefError(f"{msg.full_name}: unknown field {field_name!r}")
        if field_value is None:
            continue
        if field_def.oneof:
            previous = seen_oneofs.get(field_def.oneof)
            if previous is not None:
                raise ProtoDefError(
                    f"{msg.full_name}: oneof {field_def.oneof!r} has both {previous!r} and {field_name!r} set"
                )
            seen_oneofs[field_def.oneof] = field_name

    for field_name, field_def in msg.fields.items():
        if field_name not in value:
            continue
        field_value = value[field_name]
        if field_value is None:
            continue

        if field_def.repeated:
            if not isinstance(field_value, (list, tuple)):
                raise ProtoDefError(f"{msg.full_name}.{field_name}: repeated field value must be a list")
            if field_def.packed:
                out += _encode_packed_field(schema, field_def, field_value)
            else:
                for item in field_value:
                    if item is None:
                        continue
                    out += _encode_field(schema, field_def, item)
        else:
            out += _encode_field(schema, field_def, field_value)

    return bytes(out)


def _encode_field(schema: Schema, field_def: FieldDef, value: Any) -> bytes:
    kind = schema.resolve_type(field_def.kind, current_namespace=field_def.owner_namespace)
    wire_type = _wire_type_for_kind(kind)
    return _encode_key(field_def.tag, wire_type) + _encode_value(schema, field_def, kind, value)


def _encode_packed_field(schema: Schema, field_def: FieldDef, values: Iterable[Any]) -> bytes:
    kind = schema.resolve_type(field_def.kind, current_namespace=field_def.owner_namespace)
    if kind not in PACKABLE_TYPES:
        raise ProtoDefError(f"{field_def.owner}.{field_def.name}: type {kind!r} cannot be packed")
    payload = bytearray()
    for value in values:
        if value is None:
            continue
        payload += _encode_value(schema, field_def, kind, value, packed_scalar=True)
    return _encode_key(field_def.tag, WIRE_LEN) + _encode_varint(len(payload)) + payload


def _encode_value(schema: Schema, field_def: FieldDef, kind: str, value: Any, *, packed_scalar: bool = False) -> bytes:
    if kind == "bool":
        return _encode_varint(1 if bool(value) else 0)
    if kind == "uint32":
        return _encode_varint(_require_int_range(value, 0, (1 << 32) - 1, field_def))
    if kind == "uint64":
        return _encode_varint(_require_int_range(value, 0, (1 << 64) - 1, field_def))
    if kind == "int32":
        v = _require_int_range(value, -(1 << 31), (1 << 31) - 1, field_def)
        return _encode_varint(v & ((1 << 64) - 1) if v < 0 else v)
    if kind == "int64":
        v = _require_int_range(value, -(1 << 63), (1 << 63) - 1, field_def)
        return _encode_varint(v & ((1 << 64) - 1) if v < 0 else v)
    if kind == "sint32":
        v = _require_int_range(value, -(1 << 31), (1 << 31) - 1, field_def)
        return _encode_varint(_zigzag_encode(v, 32))
    if kind == "sint64":
        v = _require_int_range(value, -(1 << 63), (1 << 63) - 1, field_def)
        return _encode_varint(_zigzag_encode(v, 64))
    if kind == "enum":
        return _encode_varint(_require_int_range(value, -(1 << 31), (1 << 31) - 1, field_def) & ((1 << 64) - 1))

    if kind == "fixed32":
        return struct.pack("<I", _require_int_range(value, 0, (1 << 32) - 1, field_def))
    if kind == "sfixed32":
        return struct.pack("<i", _require_int_range(value, -(1 << 31), (1 << 31) - 1, field_def))
    if kind == "fixed64":
        return struct.pack("<Q", _require_int_range(value, 0, (1 << 64) - 1, field_def))
    if kind == "sfixed64":
        return struct.pack("<q", _require_int_range(value, -(1 << 63), (1 << 63) - 1, field_def))
    if kind == "float":
        return struct.pack("<f", float(value))
    if kind == "double":
        return struct.pack("<d", float(value))

    if kind == "string":
        if not isinstance(value, str):
            raise ProtoDefError(f"{field_def.owner}.{field_def.name}: expected string")
        payload = value.encode("utf-8")
        return _encode_varint(len(payload)) + payload

    if kind == "bytes":
        payload = _coerce_bytes(value, field_def)
        return _encode_varint(len(payload)) + payload

    # Message reference.
    child_msg = schema.get_message(kind, current_namespace=field_def.owner_namespace)
    if not isinstance(value, Mapping):
        raise ProtoDefError(f"{field_def.owner}.{field_def.name}: expected object for message {kind}")
    payload = _encode_message(schema, child_msg, value)
    return _encode_varint(len(payload)) + payload


def _wire_type_for_kind(kind: str) -> int:
    if kind in VARINT_TYPES:
        return WIRE_VARINT
    if kind in FIXED64_TYPES:
        return WIRE_64BIT
    if kind in FIXED32_TYPES:
        return WIRE_32BIT
    if kind in LEN_TYPES or kind not in PRIMITIVE_TYPES:
        return WIRE_LEN
    raise ProtoDefError(f"unknown kind {kind!r}")


def _encode_key(tag: int, wire_type: int) -> bytes:
    return _encode_varint((tag << 3) | wire_type)


def _encode_varint(value: int) -> bytes:
    if not isinstance(value, int):
        raise ProtoDefError(f"varint value must be int, got {type(value).__name__}")
    if value < 0:
        raise ProtoDefError("varint value must be non-negative after normalization")
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def _require_int_range(value: Any, low: int, high: int, field_def: FieldDef) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtoDefError(f"{field_def.owner}.{field_def.name}: expected int in range [{low}, {high}]")
    if not (low <= value <= high):
        raise ProtoDefError(f"{field_def.owner}.{field_def.name}: integer {value} outside range [{low}, {high}]")
    return value


def _zigzag_encode(value: int, bits: int) -> int:
    return (value << 1) ^ (value >> (bits - 1))


def _coerce_bytes(value: Any, field_def: FieldDef) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, list):
        try:
            return bytes(value)
        except ValueError as exc:
            raise ProtoDefError(f"{field_def.owner}.{field_def.name}: invalid byte list") from exc
    if isinstance(value, Mapping):
        if "base64" in value:
            return base64.b64decode(str(value["base64"]), validate=True)
        if "hex" in value:
            return bytes.fromhex(str(value["hex"]))
    if isinstance(value, str):
        if value.startswith("base64:"):
            return base64.b64decode(value[len("base64:") :], validate=True)
        if value.startswith("hex:"):
            return bytes.fromhex(value[len("hex:") :])
        # Convenient for debugging; use base64:/hex: when exact JSON bytes matter.
        return value.encode("utf-8")
    raise ProtoDefError(f"{field_def.owner}.{field_def.name}: expected bytes-like value")


# Decoding implementation.

def _decode_message(schema: Schema, msg: MessageDef, data: bytes, *, include_unknown: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    unknown: list[dict[str, Any]] = []
    oneof_seen: dict[str, str] = {}
    offset = 0

    while offset < len(data):
        key, offset = _read_varint(data, offset)
        tag = key >> 3
        wire_type = key & 0x07
        if tag <= 0:
            raise ProtoDefError(f"{msg.full_name}: invalid tag 0 at offset {offset}")

        field_def = msg.by_tag.get(tag)
        if field_def is None:
            raw_start = offset
            offset = _skip_unknown(data, offset, wire_type)
            if include_unknown:
                unknown.append({"tag": tag, "wire": wire_type, "raw": "hex:" + data[raw_start:offset].hex()})
            continue

        value, offset = _decode_field_value(schema, field_def, wire_type, data, offset, include_unknown=include_unknown)

        if field_def.oneof:
            previous_name = oneof_seen.get(field_def.oneof)
            if previous_name and previous_name != field_def.name:
                out.pop(previous_name, None)
            oneof_seen[field_def.oneof] = field_def.name

        if field_def.repeated:
            existing = out.setdefault(field_def.name, [])
            if not isinstance(existing, list):
                raise ProtoDefError(f"{msg.full_name}.{field_def.name}: internal repeated decode error")
            if field_def.packed and isinstance(value, list):
                existing.extend(value)
            else:
                existing.append(value)
        else:
            out[field_def.name] = value

    if include_unknown and unknown:
        out["_unknown"] = unknown
    return out


def _decode_field_value(
    schema: Schema,
    field_def: FieldDef,
    wire_type: int,
    data: bytes,
    offset: int,
    *,
    include_unknown: bool,
) -> tuple[Any, int]:
    kind = schema.resolve_type(field_def.kind, current_namespace=field_def.owner_namespace)

    if field_def.packed and wire_type == WIRE_LEN:
        if kind not in PACKABLE_TYPES:
            raise ProtoDefError(f"{field_def.owner}.{field_def.name}: non-packable packed field")
        payload, offset = _read_len(data, offset)
        values = []
        inner = 0
        while inner < len(payload):
            value, inner = _decode_scalar_value(kind, payload, inner, _wire_type_for_kind(kind), field_def)
            values.append(value)
        return values, offset

    expected_wire = _wire_type_for_kind(kind)
    if wire_type != expected_wire:
        raise ProtoDefError(
            f"{field_def.owner}.{field_def.name}: expected wire type {expected_wire}, got {wire_type}"
        )

    if kind in PRIMITIVE_TYPES:
        return _decode_scalar_value(kind, data, offset, wire_type, field_def)

    payload, offset = _read_len(data, offset)
    child_msg = schema.get_message(kind, current_namespace=field_def.owner_namespace)
    return _decode_message(schema, child_msg, payload, include_unknown=include_unknown), offset


def _decode_scalar_value(kind: str, data: bytes, offset: int, wire_type: int, field_def: FieldDef) -> tuple[Any, int]:
    if kind == "bool":
        raw, offset = _read_varint(data, offset)
        return bool(raw), offset
    if kind in {"uint32", "uint64", "enum"}:
        raw, offset = _read_varint(data, offset)
        if kind == "uint32":
            raw &= (1 << 32) - 1
        return raw, offset
    if kind == "int32":
        raw, offset = _read_varint(data, offset)
        raw &= (1 << 32) - 1
        if raw >= (1 << 31):
            raw -= 1 << 32
        return raw, offset
    if kind == "int64":
        raw, offset = _read_varint(data, offset)
        raw &= (1 << 64) - 1
        if raw >= (1 << 63):
            raw -= 1 << 64
        return raw, offset
    if kind == "sint32":
        raw, offset = _read_varint(data, offset)
        return _zigzag_decode(raw), offset
    if kind == "sint64":
        raw, offset = _read_varint(data, offset)
        return _zigzag_decode(raw), offset

    if kind == "fixed32":
        _require_available(data, offset, 4)
        return struct.unpack_from("<I", data, offset)[0], offset + 4
    if kind == "sfixed32":
        _require_available(data, offset, 4)
        return struct.unpack_from("<i", data, offset)[0], offset + 4
    if kind == "fixed64":
        _require_available(data, offset, 8)
        return struct.unpack_from("<Q", data, offset)[0], offset + 8
    if kind == "sfixed64":
        _require_available(data, offset, 8)
        return struct.unpack_from("<q", data, offset)[0], offset + 8
    if kind == "float":
        _require_available(data, offset, 4)
        value = struct.unpack_from("<f", data, offset)[0]
        return value, offset + 4
    if kind == "double":
        _require_available(data, offset, 8)
        value = struct.unpack_from("<d", data, offset)[0]
        return value, offset + 8

    if kind == "string":
        payload, offset = _read_len(data, offset)
        try:
            return payload.decode("utf-8"), offset
        except UnicodeDecodeError as exc:
            raise ProtoDefError(f"{field_def.owner}.{field_def.name}: invalid UTF-8 string") from exc

    if kind == "bytes":
        payload, offset = _read_len(data, offset)
        return payload, offset

    raise ProtoDefError(f"{field_def.owner}.{field_def.name}: cannot decode unknown primitive {kind!r}")


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    result = 0
    start = offset
    while True:
        if offset >= len(data):
            raise ProtoDefError(f"truncated varint at offset {start}")
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, offset
        shift += 7
        if shift >= 70:
            raise ProtoDefError(f"varint too long at offset {start}")


def _read_len(data: bytes, offset: int) -> tuple[bytes, int]:
    size, offset = _read_varint(data, offset)
    _require_available(data, offset, size)
    return data[offset : offset + size], offset + size


def _skip_unknown(data: bytes, offset: int, wire_type: int) -> int:
    if wire_type == WIRE_VARINT:
        _, offset = _read_varint(data, offset)
        return offset
    if wire_type == WIRE_64BIT:
        _require_available(data, offset, 8)
        return offset + 8
    if wire_type == WIRE_LEN:
        _, offset = _read_len(data, offset)
        return offset
    if wire_type == WIRE_32BIT:
        _require_available(data, offset, 4)
        return offset + 4
    raise ProtoDefError(f"unsupported/invalid wire type {wire_type}")


def _require_available(data: bytes, offset: int, size: int) -> None:
    if size < 0 or offset + size > len(data):
        raise ProtoDefError("truncated protobuf payload")


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)
