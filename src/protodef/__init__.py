"""Minimal `.protodef` protobuf wire encoder/decoder."""

from .protodef import (
    ProtoDefError,
    Schema,
    decode_message,
    encode_message,
    load_schema,
)

__all__ = [
    "ProtoDefError",
    "Schema",
    "decode_message",
    "encode_message",
    "load_schema",
]
